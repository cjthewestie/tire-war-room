"""後台 稽核紀錄 (read-only audit viewer): filter and page the audit file newest first, export a range as JSONL.

The file is read backwards in blocks and each line is pre-filtered on its raw text (kind, free text, seq from the
line head) before it is parsed, so a page of 100 out of 10k+ entries parses little more than the page. Factory
time is not monotonic (the day loop wraps 24:00 -> 00:00), so a time range filters but never stops the scan.
Nothing here writes the log except the caller's own audit of an export."""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from warroom.contracts import AuditKind
from warroom.factory import TZ

PAGE = 100
EXPORT_MAX = 100_000
_SEQ = re.compile(rb'^\{"seq":(\d+),')

KIND_LABEL: dict[str, str] = {
    AuditKind.STATE_TRANSITION: "狀態轉換", AuditKind.LLM_CALL: "模型呼叫", AuditKind.TOOL_CALL: "工具呼叫",
    AuditKind.TOOL_RESULT: "工具結果", AuditKind.GATE: "動作把關", AuditKind.INTERCEPTED: "攔截",
    AuditKind.APPROVAL_REQUEST: "送核准", AuditKind.APPROVAL_DECISION: "核准決定", AuditKind.SAFETY_ACTION: "安全核心動作",
    AuditKind.ANOMALY: "異常", AuditKind.SCHEDULE_VERSION: "排程版本", AuditKind.ROLLBACK: "回復",
    AuditKind.NOTIFY: "通知", AuditKind.SYSTEM: "系統", AuditKind.TASK: "現場任務",
}


def kinds() -> list[dict[str, str]]:
    return [{"id": k.value, "label": KIND_LABEL.get(k, k.value)} for k in AuditKind]


def _lines_reverse(path: Path, block: int = 1 << 16) -> Iterator[bytes]:
    """Complete lines of the file, last first."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        pos, rest = f.tell(), b""
        while pos > 0:
            n = min(block, pos)
            pos -= n
            f.seek(pos)
            buf = f.read(n) + rest
            parts = buf.split(b"\n")
            rest = parts[0]
            for ln in reversed(parts[1:]):
                if ln.strip():
                    yield ln
        if rest.strip():
            yield rest


def _ts(s: str | None) -> datetime | None:
    """ISO time; a '+' turned into a space by a query string is put back; no zone = factory time."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "+"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=TZ)


class Filter:
    def __init__(self, kind: str = "", actor: str = "", event: str = "", q: str = "", t_from: str = "", t_to: str = ""):
        self.kind = kind.strip()
        self.actor = actor.strip().lower()
        self.event = event.strip().lower()
        self.q = q.strip().lower()
        self.t_from, self.t_to = _ts(t_from), _ts(t_to)
        self._kind_b = f'"kind":"{self.kind}"'.encode() if self.kind else None
        self._q_b = self.q.encode("utf-8") if self.q else None

    def raw_ok(self, line: bytes) -> bool:
        if self._kind_b is not None and self._kind_b not in line:
            return False
        return self._q_b is None or self._q_b in line.lower()

    def ok(self, d: dict[str, Any]) -> bool:
        if self.kind and d.get("kind") != self.kind:
            return False
        if self.actor and self.actor not in str(d.get("actor") or "").lower():
            return False
        if self.event and self.event not in str(d.get("event_id") or "").lower():
            return False
        if self.t_from or self.t_to:
            t = _ts(d.get("ts"))
            if t is None:
                return False
            if (self.t_from and t < self.t_from) or (self.t_to and t > self.t_to):
                return False
        return True

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "actor": self.actor, "event": self.event, "q": self.q,
                "from": self.t_from.isoformat() if self.t_from else None, "to": self.t_to.isoformat() if self.t_to else None}


def source(audit: Any) -> tuple[Callable[[], Iterable[bytes]], Callable[[], Iterable[bytes]]]:
    """(newest-first lines, oldest-first lines) of an audit log: its file when it has one, else (the in-memory
    fallback log) its entries serialised the way the file writes them."""
    path = getattr(audit, "path", None)
    if path is not None:
        p = Path(path)
        return (lambda: _lines_reverse(p) if p.exists() else iter(())), (lambda: open(p, "rb") if p.exists() else iter(()))

    def mem() -> list[bytes]:
        return [e.model_dump_json().encode("utf-8") for e in audit.tail(0, 10**9)]
    return (lambda: reversed(mem())), mem


def page(newest_first: Iterable[bytes], flt: Filter, cursor: int | None = None, limit: int = PAGE) -> dict[str, Any]:
    """Entries with seq < cursor (all when None) that match, newest first. next_cursor is set when more remain."""
    rows: list[dict[str, Any]] = []
    scanned = 0
    more = False
    for ln in newest_first:
        m = _SEQ.match(ln)
        if cursor is not None and m and int(m.group(1)) >= cursor:
            continue
        scanned += 1
        if not flt.raw_ok(ln):
            continue
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if not flt.ok(d):
            continue
        if len(rows) == limit:
            more = True
            break
        rows.append(row(d))
    return {"rows": rows, "next_cursor": rows[-1]["seq"] if more and rows else None, "scanned": scanned,
            "filter": flt.as_dict(), "page_size": limit}


def export_lines(oldest_first: Iterable[bytes], flt: Filter, limit: int = EXPORT_MAX) -> tuple[list[str], bool]:
    """The matching lines exactly as written (hashes intact), oldest first. (lines, cut) where cut = more than limit."""
    out: list[str] = []
    cut = False
    try:
        for ln in oldest_first:
            if not ln.strip() or not flt.raw_ok(ln):
                continue
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            if flt.ok(d):
                if len(out) == limit:
                    cut = True
                    break
                out.append(ln.decode("utf-8").rstrip("\n"))
    finally:
        close = getattr(oldest_first, "close", None)
        if close:
            close()
    return out, cut


def row(d: dict[str, Any]) -> dict[str, Any]:
    kind = str(d.get("kind") or "")
    out = {"seq": d.get("seq"), "ts": d.get("ts"), "kind": kind, "kind_label": KIND_LABEL.get(kind, kind),
           "actor": d.get("actor"), "event_id": d.get("event_id"), "summary": summary(kind, str(d.get("actor") or ""),
                                                                                       d.get("payload") or {}),
           "payload": d.get("payload") or {}, "hash": d.get("hash"), "prev_hash": d.get("prev_hash")}
    p = out["payload"]
    if kind == AuditKind.LLM_CALL:
        out["llm"] = {"model": p.get("model"), "prompt_tokens": p.get("prompt_tokens"),
                      "completion_tokens": p.get("completion_tokens"), "total_ms": p.get("total_ms"),
                      "plant_sha256": p.get("plant_sha256")}
    if d.get("actor") == "safety_filter":
        out["filter"] = {"category": p.get("category"), "matched": p.get("matched"), "side": p.get("side")}
    return out


def _short(v: Any, n: int = 60) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[: n - 1] + "…"


def _kv(p: dict[str, Any], n: int = 110) -> str:
    """key: value pairs in words, not a JSON dump."""
    return _short("・".join(f"{k} {_short(v, 40)}" for k, v in p.items() if v not in (None, "", [], {})), n)


def summary(kind: str, actor: str, p: dict[str, Any]) -> str:
    """One plain line per entry; the full payload is one click away."""
    try:
        if kind == AuditKind.LLM_CALL:
            ms = p.get("total_ms")
            tok = f"{p.get('prompt_tokens')}→{p.get('completion_tokens')} tokens" if p.get("prompt_tokens") is not None else ""
            bits = [p.get("phase") or "", p.get("model") or "", f"{round(ms)} ms" if isinstance(ms, (int, float)) else "", tok]
            if p.get("error"):
                bits.append(f"失敗：{_short(p.get('reason') or p['error'], 50)}")
            elif p.get("fell_back_to_mock"):
                bits.append("模型輸出沒通過，改用規則計劃")
            return "・".join(b for b in bits if b)
        if kind == AuditKind.TOOL_CALL:
            return f"{actor.removeprefix('tool:')}（{_short(p.get('args') or {}, 70)}）"
        if kind == AuditKind.TOOL_RESULT:
            ms = p.get("duration_ms")
            return f"{actor.removeprefix('tool:')} {'成功' if p.get('ok', True) else '失敗：' + _short(p.get('error') or '', 50)}" + \
                (f"・{round(ms)} ms" if isinstance(ms, (int, float)) else "")
        if kind == AuditKind.GATE:
            dec = p.get("decision") or {}
            return f"{dec.get('step_id') or p.get('step_id') or ''} {dec.get('tool') or ''}・{dec.get('risk') or ''}・{dec.get('verdict') or ''}".strip()
        if kind == AuditKind.STATE_TRANSITION:
            return f"{p.get('from') or '—'} → {p.get('to')}" + (f"（{_short(p['reason'], 50)}）" if p.get("reason") else "")
        if kind == AuditKind.APPROVAL_REQUEST:
            acts = p.get("actions") or []
            return f"送核准 {len(acts)} 項：{_short(acts[0], 60)}" if acts else "送核准"
        if kind == AuditKind.APPROVAL_DECISION:
            return f"{p.get('decision')}・{p.get('request_id') or ''}" + (f"・{_short(p['reason'], 40)}" if p.get("reason") else "")
        if kind in (AuditKind.SAFETY_ACTION, AuditKind.ANOMALY):
            return _short(p.get("message") or p.get("msg") or p, 100)
        if kind == AuditKind.TASK:
            return f"{p.get('task_id') or ''} {p.get('change') or ''}・{_short(p.get('title') or '', 60)}"
        if actor == "safety_filter":
            return f"擋下敏感詞（{p.get('category')}・{p.get('side')}）：{p.get('matched')}"
        for k in ("msg", "admin", "orch"):
            if p.get(k):
                rest = {kk: vv for kk, vv in p.items() if kk != k}
                return f"{p[k]}" + (f"・{_kv(rest)}" if rest else "")
        return _kv(p)
    except Exception:  # noqa: BLE001 - a summary must never hide a row
        pass
    return _short(p, 110)
