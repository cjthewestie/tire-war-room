"""What the plant remembers about one machine, for the agents (CJ, 2026-10-22: 「接記憶」).

Two sources, both read-only here:
- the supervisor's memory notes from 後台 (warroom/data/memory_notes.py), newest first;
- past events on that machine: events that closed (the audit log is the record; a live event adds its hypothesis,
  goal and outcome) and the team dataset's incidents (warroom/data/team_dataset.py IncidentStore).

`for_machine()` is the compact block the commander gets as context.machine_memory and Agent Nemo gets from
query_plant machine_history; `closed_events()` / `incidents()` are what 後台 記憶 lists. Nothing here writes."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from warroom import factory as F
from warroom.contracts import AuditKind, HypothesisStatus

NOTE_LIMIT = 5
NOTE_CHARS = 120
PAST_LIMIT = 3
FIELD_CHARS = 60
MAX_BLOCK_CHARS = 2000      # the whole block as JSON: the commander's per-event context stays small however much is remembered
# one line in the block itself (the user message, never the system prompt: that stays byte-identical)
USE = "機台筆記與過去事件只供參考假設，數字仍以工具結果為準"
TERMINAL = ("closed", "safe_stop")
_EV_MACHINE_RE = re.compile(r"(" + "|".join(re.escape(m) for m in sorted(F.MACHINE_IDS, key=len, reverse=True)) + r")$")


def machine_of_event(event_id: str | None) -> str | None:
    m = _EV_MACHINE_RE.search(event_id or "")
    return m.group(1) if m else None


def _cut(s: Any, n: int = FIELD_CHARS) -> str | None:
    if s is None or s == "":
        return None
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _day(t: Any) -> str:
    if isinstance(t, datetime):
        return t.astimezone(F.TZ).date().isoformat()
    if isinstance(t, date):
        return t.isoformat()
    return str(t)[:10]


def incidents() -> list[dict[str, Any]]:
    """The team dataset's past incidents (empty when the dataset is not in this checkout)."""
    try:
        from warroom.data.team_dataset import IncidentStore
        rows = IncidentStore().all()
    except Exception:  # noqa: BLE001 - no dataset: that source is left out
        return []
    return [{"id": i.incident_id, "date": i.date.isoformat(), "machine_id": i.machine_id, "equipment": i.equipment,
             "symptom": i.symptom, "action": i.action, "downtime_min": i.downtime_min, "outcome": i.outcome,
             "source": "team_dataset"} for i in rows]


def closed_events(audit: Any, events: list[Any]) -> list[dict[str, Any]]:
    """Every event that reached closed / safe_stop, newest first. The audit log is the record (it outlives a scenario
    restart); a live event adds what only it knows (the confirmed hypothesis, its goal)."""
    live = {ev.id: ev for ev in events}
    closed: dict[str, dict[str, Any]] = {}
    anomaly: dict[str, str] = {}
    causes: dict[str, dict[str, str]] = {}          # event -> hypothesis id -> cause_code
    verdict: dict[str, tuple[str, str]] = {}        # event -> (hypothesis id, verdict) of the last check
    for e in audit.tail(0, 100_000):
        p = e.payload or {}
        if not e.event_id or e.event_id.startswith("ASK-"):
            continue
        if e.kind == AuditKind.ANOMALY and p.get("message"):
            anomaly.setdefault(e.event_id, str(p["message"]))
        elif e.kind == AuditKind.LLM_CALL and e.actor == "commander" and p.get("hypothesis") and p.get("cause_code"):
            causes.setdefault(e.event_id, {})[str(p["hypothesis"])] = str(p["cause_code"])
        elif e.kind == AuditKind.SYSTEM and e.actor == "verifier" and p.get("verdict"):
            verdict[e.event_id] = (str(p.get("hypothesis")), str(p["verdict"]))
        elif e.kind == AuditKind.STATE_TRANSITION and p.get("to") in TERMINAL:
            ev = live.get(e.event_id)
            closed[e.event_id] = {"id": e.event_id, "machine_id": ev.machine_id if ev else machine_of_event(e.event_id),
                                  "closed_at": e.ts.isoformat(), "state": p.get("to"),
                                  "what": (ev.command or (ev.alert_card.title if ev.alert_card else None)) if ev else None,
                                  "outcome": ev.outcome if ev else None, "source": "audit"}
    for cid, c in closed.items():
        c["what"] = c["what"] or anomaly.get(cid)
        ev = live.get(cid)
        c["symptom"] = ev.anomaly.message if ev else anomaly.get(cid)
        c["cause"], c["action"] = _cause(ev, causes.get(cid, {}), verdict.get(cid)), ev.goal if ev else None
    return sorted(closed.values(), key=lambda c: c["closed_at"], reverse=True)


def _cause(ev: Any, causes: dict[str, str], last: tuple[str, str] | None) -> str | None:
    """The confirmed hypothesis when there is one, else the last one tested and how the check came out."""
    if ev is not None and ev.hypotheses:
        h = next((h for h in ev.hypotheses if h.status is HypothesisStatus.CONFIRMED), None)
        tag = "確認" if h else None
        h = h or ev.hypotheses[-1]
        tag = tag or {"rejected": "推翻", "active": "未驗證"}.get(h.status.value, "未驗證")
        return f"{h.id} {h.statement}（{tag}）"
    if last:
        hid, v = last
        word = {"confirmed": "確認", "rejected": "推翻", "inconclusive": "無法驗證"}.get(v, v)
        return f"{hid} {causes.get(hid, '')}（{word}）".replace("  ", " ")
    return next((f"{k} {v}" for k, v in reversed(causes.items())), None)


def past_for(machine_id: str, audit: Any, events: list[Any], exclude: str | None = None,
             limit: int = PAST_LIMIT) -> list[dict[str, Any]]:
    """The last `limit` past events on one machine, closed events and dataset incidents together, newest first."""
    rows = [{"id": c["id"], "date": _day(c["closed_at"]), "symptom": _cut(c.get("symptom") or c.get("what")),
             "cause": _cut(c.get("cause")), "action": _cut(c.get("action")), "outcome": _cut(c.get("outcome")),
             "source": "audit"}
            for c in closed_events(audit, events) if c["machine_id"] == machine_id and c["id"] != exclude]
    rows += [{"id": i["id"], "date": i["date"], "symptom": _cut(i["symptom"]), "cause": None, "action": _cut(i["action"]),
              "outcome": _cut(f"{i['outcome']}（停機 {i['downtime_min']} 分）"), "source": "team_dataset"}
             for i in incidents() if i["machine_id"] == machine_id]
    rows.sort(key=lambda r: r["date"], reverse=True)
    return [{k: v for k, v in r.items() if v is not None} for r in rows[:limit]]


def notes_for(machine_id: str, notes: Any, limit: int = NOTE_LIMIT) -> list[dict[str, Any]]:
    if notes is None:
        return []
    try:
        mine = notes.for_machine(machine_id)
    except Exception:  # noqa: BLE001 - an unreadable notes file is no notes
        return []
    return [{"id": n.id, "date": _day(n.created_at), "by": n.author, "text": _cut(n.text, NOTE_CHARS)}
            for n in mine[:limit]]


def for_machine(machine_id: str, notes: Any, audit: Any, events: list[Any], exclude: str | None = None,
                files: Any = None, levels: tuple[str, ...] = ("daily",)) -> dict[str, Any] | None:
    """{machine_id, use, notes, past, days} for one machine, or None when there is nothing to remember. `days` is that
    machine's line from the last 3 daily summaries (`files`: warroom/data/memory_log.py MemoryFiles; the levels
    weekly / monthly add the longer timeline for query_plant machine_history). At most MAX_BLOCK_CHARS as JSON."""
    import json
    if machine_id not in F.MACHINE_BY_ID:
        return None
    n, past = notes_for(machine_id, notes), past_for(machine_id, audit, events, exclude)
    days = []
    if files is not None:
        try:
            days = [{"from": f"{x['level']} {x['label']}", "line": x["line"]} for x in files.machine_lines(machine_id, levels)]
        except Exception:  # noqa: BLE001 - summaries are optional
            days = []
    if not n and not past and not days:
        return None
    block = {"machine_id": machine_id, "use": USE, "notes": n, "past": past, **({"days": days} if days else {})}
    size = lambda b: len(json.dumps(b, ensure_ascii=False))
    while size(block) > MAX_BLOCK_CHARS and block.get("days"):
        block["days"] = block["days"][:-1] or None
        if block["days"] is None:
            del block["days"]
    while size(block) > MAX_BLOCK_CHARS and len(block["notes"]) > 1:
        block["notes"] = block["notes"][:-1]
    return block


def attached(block: dict[str, Any] | None) -> dict[str, Any]:
    """What an audit row records: which note and past-event ids went into a request."""
    if not block:
        return {"note_ids": [], "past_event_ids": []}
    return {"note_ids": [x["id"] for x in block.get("notes", [])], "past_event_ids": [x["id"] for x in block.get("past", [])]}


def timeline_line(block: dict[str, Any]) -> str:
    """「參考了 7 號機筆記 1 則、過去事件 2 件」"""
    from warroom.agent.narrate import machine_label
    parts = [f"筆記 {len(block['notes'])} 則" if block.get("notes") else None,
             f"過去事件 {len(block['past'])} 件" if block.get("past") else None]
    return f"參考了 {machine_label(block['machine_id'])}" + "、".join(p for p in parts if p)
