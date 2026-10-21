"""每日事件紀錄 (issue #37; CJ, 2026-10-20: 「狀態機也需要記錄每天的事件」): every event, supervisor task and question,
grouped by factory day, rebuilt from the audit log only (read-only).

A day is the factory date of the entry (Asia/Taipei). When the one-day demo loop writes its marker (system entry,
actor "day_loop", payload {"loop": n, "msg": "新的一天（循環 n）"}), days are the loops instead: everything before the
first marker is 「循環 0」. Each run: opened time, machine, type, the states it went through with times, rounds,
outcome, approvals (decision, who, via) and duration."""
from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from warroom import factory as F
from warroom.contracts import AuditEntry, AuditKind

TERMINAL = ("closed", "safe_stop")
_MACHINE_RE = re.compile(r"(" + "|".join(re.escape(m) for m in sorted(F.MACHINE_IDS, key=len, reverse=True)) + r")$")
TYPE_LABEL = {"event": "機台異常", "command": "主管任務", "plant": "工廠事件", "ask": "問戰情室", "main": "NemoClaw 對話"}


def _day(ts: datetime) -> str:
    return ts.astimezone(F.TZ).date().isoformat()


def group_key(entries: list[AuditEntry]) -> list[str]:
    """The day key of every entry, in order: 'YYYY-MM-DD', or 'loop-n' once a day-loop marker exists."""
    loops = any(e.actor == "day_loop" and "loop" in (e.payload or {}) for e in entries)
    out, cur = [], "loop-0"
    for e in entries:
        if loops:
            if e.actor == "day_loop" and "loop" in (e.payload or {}):
                cur = f"loop-{int(e.payload['loop'])}"
            out.append(cur)
        else:
            out.append(_day(e.ts))
    return out


def _run(eid: str) -> dict[str, Any]:
    m = _MACHINE_RE.search(eid)
    return {"id": eid, "type": "ask" if eid.startswith("ASK-") else "main" if eid.startswith("MAIN-") else "event", "machine_id": m.group(1) if m else None,
            "opened_at": None, "closed_at": None, "path": [], "rounds": 1, "outcome": "open", "approvals": [],
            "pending_approvals": 0, "title": None, "duration_min": None}


def build(entries: Iterable[AuditEntry], sources: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """{day key: {"key", "label", "first", "last", "runs": [...]}} from audit entries (any order)."""
    es = sorted(entries, key=lambda e: e.seq)
    keys = group_key(es)
    days: dict[str, dict[str, Any]] = {}
    runs: dict[str, dict[str, Any]] = {}
    run_day: dict[str, str] = {}
    requested: dict[str, set[str]] = {}
    for e, k in zip(es, keys):
        d = days.setdefault(k, {"key": k, "label": None, "first": e.ts, "last": e.ts, "last_seq": e.seq, "runs": []})
        d["first"], d["last"], d["last_seq"] = min(d["first"], e.ts), max(d["last"], e.ts), e.seq
        if e.actor == "day_loop" and "loop" in (e.payload or {}):
            d["label"] = str(e.payload.get("msg") or f"新的一天（循環 {e.payload['loop']}）")
        eid = e.event_id
        if not eid:
            continue
        p = e.payload or {}
        if eid not in runs:
            runs[eid] = _run(eid)
            run_day[eid] = k
            days[k]["runs"].append(runs[eid])
        r = runs[eid]
        r["opened_at"] = r["opened_at"] or e.ts
        if sources and eid in sources:
            r["type"] = sources[eid] if sources[eid] in TYPE_LABEL else r["type"]
        if e.kind == AuditKind.STATE_TRANSITION and p.get("to"):
            if p.get("reason") == "supervisor command":
                r["type"] = "command"
            r["path"].append({"state": str(p["to"]), "ts": e.ts})
            if p["to"] == "replan":
                r["rounds"] += 1
            if p["to"] in TERMINAL:
                r["outcome"], r["closed_at"] = str(p["to"]), e.ts
        elif e.kind == AuditKind.ANOMALY and p.get("message"):
            r["title"] = r["title"] or str(p["message"])
            r["machine_id"] = r["machine_id"] or p.get("machine_id")
        elif e.kind == AuditKind.SYSTEM:
            if isinstance(p.get("command"), str):
                r["type"], r["title"] = "command", r["title"] or p["command"]
                r["machine_id"] = r["machine_id"] or p.get("machine_id")
            if p.get("msg") == "ask":
                r["title"] = str(p.get("question") or "")
                r["by"] = e.actor
            if p.get("msg") == "answered" and r["type"] == "ask":
                r["outcome"], r["closed_at"] = "answered", e.ts
        elif e.kind == AuditKind.APPROVAL_REQUEST:
            requested.setdefault(eid, set()).add(str(p.get("id") or e.seq))
            r["title"] = r["title"] or (str(p["title"]) if p.get("title") else None)
        elif e.kind == AuditKind.APPROVAL_DECISION:
            requested.get(eid, set()).discard(str(p.get("request_id") or ""))
            r["approvals"].append({"decision": p.get("decision"), "by": e.actor, "via": p.get("via"),
                                   "option": p.get("option_id"), "round": p.get("round"), "ts": e.ts})
    for eid, r in runs.items():
        if r["path"] and r["path"][0]["state"] != "detected":   # a detector event opens in DETECTED without a row
            r["path"].insert(0, {"state": "detected", "ts": r["opened_at"]})
        r["pending_approvals"] = len(requested.get(eid, ()))
        if r["opened_at"] and r["closed_at"]:
            r["duration_min"] = round((r["closed_at"] - r["opened_at"]).total_seconds() / 60, 1)
    for d in days.values():
        if not d["label"]:
            d["label"] = d["key"] if not d["key"].startswith("loop-") else f"循環 {d['key'][5:]}（開機到第一次換日）"
    return days


def summary(day: dict[str, Any]) -> dict[str, Any]:
    runs = day["runs"]
    closed = [r for r in runs if r["duration_min"] is not None and r["type"] != "ask"]
    by_outcome: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for r in runs:
        by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
    return {"runs": len(runs), "by_outcome": by_outcome, "by_type": by_type,
            "avg_close_min": round(sum(r["duration_min"] for r in closed) / len(closed), 1) if closed else None,
            "approvals_decided": sum(len(r["approvals"]) for r in runs),
            "approvals_pending": sum(r["pending_approvals"] for r in runs)}


def jsonable(day: dict[str, Any]) -> dict[str, Any]:
    def conv(v: Any) -> Any:
        if isinstance(v, datetime):
            return v.isoformat()
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        if isinstance(v, list):
            return [conv(x) for x in v]
        return v
    return conv(day)


CSV_COLUMNS = ["id", "類型", "機台", "開始", "結束", "耗時(分)", "結果", "輪數", "狀態路徑", "核准", "待核准", "內容"]
OUTCOME = {"closed": "結案", "safe_stop": "安全停止", "open": "處理中", "answered": "已回答"}


def to_csv(day: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for r in day["runs"]:
        appr = "；".join(f"{a['decision']}・{a['by']}{'・' + a['via'] if a.get('via') else ''}" for a in r["approvals"])
        w.writerow([r["id"], TYPE_LABEL.get(r["type"], r["type"]), r["machine_id"] or "",
                    r["opened_at"].isoformat() if r["opened_at"] else "", r["closed_at"].isoformat() if r["closed_at"] else "",
                    r["duration_min"] if r["duration_min"] is not None else "", OUTCOME.get(r["outcome"], r["outcome"]), r["rounds"],
                    "→".join(p["state"] for p in r["path"]), appr, r["pending_approvals"], r["title"] or ""])
    return "﻿" + buf.getvalue()      # BOM: Excel opens the Chinese columns as UTF-8
