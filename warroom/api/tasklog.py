"""今天處理過的任務, never cleared (CJ, 2026-10-22: 「今天處理過的任務都要記錄在agent 頁面不可以清掉」).

Built from the audit log FILE (data/runtime/audit.jsonl), which survives a scenario load, 重來, a server restart and the
day-loop wrap; the in-memory audit keeps only its last few thousand rows, the live events list only the current
scenario. Read incrementally: each call parses only the lines added since the last one.

One row per task (an event run): opened, closed, machine or order, title, final state (結案 / 安全停止 / 主管中斷 /
人工處理), who decided which card version, outcome, and its report link. Grouped by the factory date the task opened,
then by day-loop cycle (循環 n) and the scenario that was loaded. Event ids repeat when a scenario is loaded again,
so a run is keyed by id + the load it belongs to. Read-only: nothing here deletes or edits a row."""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from warroom import factory as F

FINAL = {"closed": "結案", "safe_stop": "安全停止"}
TERMINAL = ("closed", "safe_stop")
_DECISION = {"approve": "核准", "reject": "駁回", "modify": "修改", "superseded": "作廢（改版）", "expired": "逾時"}


def _ts(v: Any) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))


class TaskLog:
    """`path`: the audit JSONL file. `entries`: a fallback source (an in-memory audit) when there is no file."""

    def __init__(self, path: Path | None = None, entries: Any = None):
        self.path = Path(path) if path else None
        self._entries = entries
        self._lock = threading.Lock()
        self._reset()

    def _reset(self) -> None:
        self._offset = 0
        self._gen = 0                     # +1 per scenario load: the same event id again is a new task
        self._scenario: str | None = None
        self._loop: int | None = None
        self._runs: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._latest: dict[str, str] = {}
        self._cards: dict[str, int] = {}  # approval request id -> card version

    # ---------------------------------------------------------------- reading
    def _new_rows(self) -> list[dict[str, Any]]:
        if self.path is not None and self.path.exists():
            size = self.path.stat().st_size
            if size < self._offset:       # a new file (fresh chain): read it from the start
                self._reset()
            with self.path.open("rb") as fh:
                fh.seek(self._offset)
                data = fh.read()
            end = data.rfind(b"\n") + 1   # a half-written last line waits for the next call
            self._offset += end
            out = []
            for line in data[:end].splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
            return out
        if self._entries is not None:
            rows = [e.model_dump(mode="json") for e in self._entries()]
            new = [r for r in rows if r["seq"] > self._offset]
            if new:
                self._offset = new[-1]["seq"]
            return new
        return []

    def refresh(self) -> None:
        with self._lock:
            for e in self._new_rows():
                self._take(e)

    def _take(self, e: dict[str, Any]) -> None:
        p = e.get("payload") or {}
        kind, actor, eid = e.get("kind"), e.get("actor"), e.get("event_id")
        if kind == "system" and p.get("msg") == "scenario loaded":
            self._gen += 1
            self._scenario, self._loop = p.get("scenario"), None
            return
        if kind == "system" and actor == "day_loop" and "loop" in p:
            self._loop = int(p["loop"])
            self._gen += 1
            return
        if kind == "approval_request" and p.get("id"):
            self._cards[str(p["id"])] = int(p.get("version") or 1)
        if not eid or eid.startswith(("ASK-", "MAIN-")):
            return                                         # chat turns are not tasks
        # rows after a load or a day wrap still belong to a run that has not ended; a closed run seen again after
        # one (the same scenario loaded again, same ids) is a new task
        last = self._latest.get(eid)
        key = last if last and (last.endswith(f"@{self._gen}") or self._runs[last]["closed_at"] is None) else f"{eid}@{self._gen}"
        r = self._runs.get(key)
        if r is None:
            self._latest[eid] = key
            r = self._runs[key] = {"id": eid, "opened_at": e["ts"], "closed_at": None, "machine_id": None, "order_id": None,
                                   "title": None, "final": "處理中", "state": "detected", "approvals": [], "outcome": None,
                                   "scenario": self._scenario, "loop": self._loop, "rounds": 1, "source": "event",
                                   "report": f"/report?event={eid}"}
            self._order.append(key)
        if kind == "anomaly":
            r["title"] = r["title"] or p.get("message")
            r["machine_id"] = r["machine_id"] or p.get("machine_id")
        elif kind == "system" and isinstance(p.get("command"), str):
            r["title"], r["source"] = r["title"] or p["command"], "command"
            from warroom.agent.ask import orders_in
            r["order_id"] = r["order_id"] or next(iter(orders_in(p["command"])), None)
        elif kind == "state_transition" and p.get("to"):
            r["state"] = str(p["to"])
            if p["to"] == "replan":
                r["rounds"] += 1
            if p["to"] in TERMINAL:
                r["closed_at"] = e["ts"]
                r["final"] = FINAL[p["to"]] if r["final"] not in ("主管中斷", "人工處理") else r["final"]
        elif kind == "approval_request":
            r["title"] = r["title"] or p.get("title")
        elif kind == "approval_decision" and p.get("decision"):
            rid = str(p.get("request_id") or "")
            r["approvals"].append({"decision": _DECISION.get(p["decision"], p["decision"]), "by": actor,
                                   "version": self._cards.get(rid, p.get("version") or 1), "round": p.get("round"),
                                   "option": p.get("option_id"), "at": e["ts"]})
        elif kind == "system" and actor == "main_agent" and p.get("orch") in ("report", "interrupt"):
            if p.get("orch") == "interrupt" or p.get("interrupted"):
                r["final"] = "主管中斷"
            out = p.get("outcome") if p.get("orch") == "report" else p.get("reason")
            if out:
                r["outcome"] = str(out)[:160]
                if "人工" in str(out) and r["final"] != "主管中斷":
                    r["final"] = "人工處理"
            r["title"] = r["title"] or p.get("title")
            r["machine_id"] = r["machine_id"] or p.get("machine_id")

    # ---------------------------------------------------------------- the view
    def rows(self) -> list[dict[str, Any]]:
        self.refresh()
        with self._lock:
            out = []
            for k in self._order:
                r = dict(self._runs[k])
                r["date"] = _ts(r["opened_at"]).astimezone(F.TZ).date().isoformat()
                out.append(r)
            return out

    def days(self) -> list[dict[str, Any]]:
        """Newest date first; inside a date, groups by (loop, scenario) in the order they happened."""
        by_date: dict[str, list[dict[str, Any]]] = {}
        for r in self.rows():
            by_date.setdefault(r["date"], []).append(r)
        out = []
        for date in sorted(by_date, reverse=True):
            groups: list[dict[str, Any]] = []
            for r in by_date[date]:
                g = groups[-1] if groups and (groups[-1]["loop"], groups[-1]["scenario"]) == (r["loop"], r["scenario"]) else None
                if g is None:
                    g = {"loop": r["loop"], "scenario": r["scenario"], "rows": []}
                    groups.append(g)
                g["rows"].append(r)
            out.append({"date": date, "count": len(by_date[date]), "groups": groups})
        return out

    def today(self) -> dict[str, Any]:
        days = self.days()
        return days[0] if days else {"date": None, "count": 0, "groups": []}
