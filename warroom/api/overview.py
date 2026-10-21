"""Agent Nemo 總覽 (CJ, 2026-10-22, about 「Agent Nemo ▸ 處理中的事件」: 「我希望這裡是獨立的對話agent 可以統合整理目前進行中
的任務 查詢過去資料還有工廠的一切…他還可以列出我覺得有疑問的agent 並且列連結給我讓我可以直接連到那個agent的任務版」).

Read only, from live state. `questionable(r)` is the code's list of tasks that need the supervisor's eye, each with
its reasons; `links(...)` turns a reply into links to task boards, only for event ids that exist (never the model's
words). The chat agent sees `questionable` as the query_plant topic of the same name and in its overview context."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from warroom.contracts import HypothesisStatus

PLANNING = ("detected", "classify", "plan", "simulate", "gate", "replan")
STUCK_PLAN_MIN = 10.0     # factory minutes in a planning state before 「AI 規劃卡住」
STUCK_MIN = 30.0          # factory minutes with no step at all (any open state but 等核准, which has its own rule)
REPLANS = 2               # 重新規劃 this many times
TASK_LATE_MIN = 5.0       # a worker task this many factory minutes past its eta (a minute late is walking noise)
MAX_LINKS = 8
STATE = {"detected": "偵測到", "classify": "分類", "plan": "擬計劃", "simulate": "模擬", "gate": "把關",
         "await_approval": "等主管核准", "execute": "執行中", "verify": "驗證中", "replan": "重新規劃",
         "closed": "已結案", "safe_stop": "安全停止"}


def _what(e: Any) -> str:
    return (e.command or (e.alert_card.title if e.alert_card else e.anomaly.message) or "")[:40]


def _min(a: datetime, b: datetime) -> float:
    return round((a - b).total_seconds() / 60, 1)


def label(e: Any) -> str:
    """「7 號機・溫度偏低」 / 「主管指令・#A-311 可能晚交…」: the link text."""
    from warroom.agent.narrate import machine_label
    who = "主管指令" if e.source == "supervisor" else "工廠事件" if e.source == "plant" else machine_label(e.machine_id)
    what = _what(e)[:24]
    return what if what.startswith(who) or who.replace("機", "") in what[:8] else f"{who}・{what}"


def _audit_flags(r: Any, ids: set[str]) -> dict[str, dict[str, int]]:
    """Per event: approvals the supervisor rejected, commander plans that fell back to rules."""
    out: dict[str, dict[str, int]] = {}
    if not ids or getattr(r, "audit", None) is None:
        return out
    try:
        rows = r.audit.tail(0, 1_000_000)
    except Exception:  # noqa: BLE001 - no audit: the state rules still answer
        return out
    for a in rows:
        if a.event_id not in ids:
            continue
        p = a.payload or {}
        k = a.kind.value
        f = out.setdefault(a.event_id, {"rejects": 0, "fallbacks": 0})
        if k == "approval_decision" and p.get("decision") == "reject":
            f["rejects"] += 1
        elif k == "llm_call" and a.actor == "commander" and (p.get("fell_back_to_mock") or p.get("plan_source") == "mock_fallback"):
            f["fallbacks"] += 1
    return out


def questionable(r: Any) -> list[dict[str, Any]]:
    """The tasks that look wrong, each with its reasons in plain words (newest first). Open events, plus a safe stop
    nobody has acted on yet."""
    now = r.clock.now()
    pending: dict[str, Any] = {}
    if getattr(r, "approvals", None) is not None:
        for a in r.approvals.pending():
            if a.event_id not in pending or a.version > pending[a.event_id].version:
                pending[a.event_id] = a
    evs = [e for e in r.events()
           if e.closed_at is None or (e.state.value == "safe_stop" and e.follow_up is None)]
    flags = _audit_flags(r, {e.id for e in evs})
    try:
        board = r.orch._board()
    except Exception:  # noqa: BLE001
        board = None
    out = []
    for e in evs:
        st = e.state.value
        why: list[str] = []
        if st == "safe_stop":
            why.append(f"安全停止，還沒有人處理（{e.stop_reason_text or e.outcome or '原因未記'}）")
        card = pending.get(e.id)
        if card is not None and card.expires_at is not None:
            total, gone = _min(card.expires_at, card.created_at), _min(now, card.created_at)
            if total > 0 and gone > total / 2:
                why.append(f"等核准 {gone:.0f} 分鐘，超過時限一半（時限 {total:.0f} 分鐘，到時沒核准會安全停止）")
        if e.replans >= REPLANS:
            why.append(f"重新規劃了 {e.replans} 次")
        for h in e.hypotheses:
            if h.status == HypothesisStatus.REJECTED:
                why.append(f"假設 {h.id} 被推翻：{h.statement[:24]}")
                break
        f = flags.get(e.id, {})
        if f.get("rejects"):
            why.append(f"主管退回過 {f['rejects']} 次計劃")
        if e.consecutive_failures:
            why.append(f"步驟／驗證連續失敗 {e.consecutive_failures} 次")
        elif any(s.state == "failed" for s in e.steps):
            why.append("有步驟失敗")
        if f.get("fallbacks"):
            why.append("模型沒回應，計劃改用規則擬的")
        last = e.timeline[-1].ts if e.timeline else e.opened_at
        idle = _min(now, last)
        if st in PLANNING and idle > STUCK_PLAN_MIN:
            why.append(f"AI 規劃卡住 {idle:.0f} 分鐘")
        elif st not in PLANNING and st not in ("await_approval", "safe_stop") and idle > STUCK_MIN:
            why.append(f"停在「{STATE.get(st, st)}」{idle:.0f} 分鐘沒有進度")
        if board is not None:
            late = [t for t in board.for_event(e.id) if t.status != "完成" and t.eta is not None and _min(now, t.eta) > TASK_LATE_MIN]
            if late:
                t = max(late, key=lambda x: _min(now, x.eta))
                why.append(f"現場任務逾時：{t.title[:24]} 晚了 {_min(now, t.eta):.0f} 分鐘" + (f"（共 {len(late)} 件）" if len(late) > 1 else ""))
        if why:
            out.append({"id": e.id, "what": _what(e), "machine": e.machine_id, "state": STATE.get(st, st),
                        "reasons": why, "opened": e.opened_at})
    out.sort(key=lambda x: x.pop("opened"), reverse=True)
    return out


def snapshot(r: Any) -> dict[str, Any]:
    """What the overview conversation always has: every open task and the questionable ones."""
    from warroom.api import plant_query
    return {"tasks": plant_query.tasks(r)["tasks"], "questionable": questionable(r)}


_ID_RX = re.compile(r"EV-[0-9A-Za-z-]+")
_ORDER_RX = re.compile(r"#\s?[A-Z]-?\d{2,4}|SO-?\d{1,3}")
_PRESS_RX = re.compile(r"(\d{1,2})\s?號機?")
QUESTIONABLE_RX = re.compile(r"有問題|有疑問|怪怪|不對勁|卡住|要注意|該注意|需要我|要我處理|異常的任務|哪些.{0,4}(?:不正常|出錯|有狀況)")


def _ids_in(data: Any) -> list[str]:
    """Event ids in a query_plant result, in order (tasks, open_events, handled_today, order_lookup, questionable)."""
    out: list[str] = []
    if isinstance(data, dict):
        for k, v in data.items():
            if k == "id" and isinstance(v, str) and v.startswith("EV-"):
                out.append(v)
            else:
                out += _ids_in(v)
    elif isinstance(data, list):
        for x in data:
            out += _ids_in(x)
    return out


def _mentioned(e: Any, reply: str) -> bool:
    if e.id in reply:
        return True
    if any(o.replace(" ", "") in reply.replace(" ", "") for o in _ORDER_RX.findall(_what(e))):
        return True
    from warroom.agent.narrate import machine_label
    m = re.match(r"CURE-0?(\d+)$", e.machine_id or "")
    if m:
        return any(n == m.group(1) for n in _PRESS_RX.findall(reply)) or e.machine_id in reply
    # 「B-OPEN」 or 「B 線開煉機」 (the model writes either)
    return bool(e.machine_id and e.machine_id != "PLANT" and (e.machine_id in reply
                                                               or machine_label(e.machine_id).replace(" ", "") in reply.replace(" ", "")))


def links(r: Any, text: str, reply: str, reads: list[tuple[str, Any]], acted: list[str] = ()) -> list[dict[str, Any]]:
    """Links to task boards for one overview reply. Only ids of events that exist: an id the model wrote is kept only
    when it is real. The questionable list (asked for, or read) is linked whole with its reasons; tasks the reads
    returned are linked when the reply names them (id, order or press), or all of them when it names none. `acted`:
    the events a control step of this turn ran on (「好」 → 中斷 it)."""
    events = {e.id: e for e in r.events()}
    asked = bool(QUESTIONABLE_RX.search(text)) or any(t == "questionable" for t, _d in reads)
    q = {x["id"]: "；".join(x["reasons"]) for x in questionable(r)} if asked else {}
    order: list[str] = [*q, *acted]
    order += [i for i in _ID_RX.findall(reply) if i in events]
    read = [i for _t, d in reads for i in _ids_in(d) if i in events]
    named = [i for i in read if _mentioned(events[i], reply)]
    order += named or read
    out, seen = [], set()
    for i in order:
        if i in seen or i not in events:
            continue
        seen.add(i)
        e = events[i]
        out.append({"event_id": i, "label": label(e), "state": STATE.get(e.state.value, e.state.value), "reason": q.get(i)})
        if len(out) >= MAX_LINKS:
            break
    return out
