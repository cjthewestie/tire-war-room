"""One Agent Nemo session per task, and the task list behind it (issue #189).

CJ, 2026-10-22: 「agent nemo 主控台的是一個任務一個 agent context，不要共用」「還有進行中任務也要可以搜尋的到 今日已結案也要
然後隔日已結案的任務會儲存到歷史任務區分每天」.

- A session is a task: its id is the event id. The main agent's memory for it is keyed `task:<event id>|<actor>`
  (app.py main_message), so one task's conversation never reaches another's; a message with no task selected goes to
  the 「新對話」 session (`<actor>|<page conversation id>`), and the 總覽 has its own (`overview|…`).
- The list: 進行中 (live events not ended, or ended with people still at work), 今日已結案 (closed in the current factory
  day: same epoch — runtime start / scenario load — same day-loop cycle, same factory date) and 歷史任務 (every other
  closed task), grouped by factory day, newest first. Built from the task log (warroom/api/tasklog.py, the audit FILE),
  so it survives the 24:00 wrap, a scenario load and a restart. Read-only.
- A session's turns: live from the audit rows on the page; for a task of an earlier day (or after a restart) from the
  chat log on disk (warroom/data/history_log.py), by event id."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from warroom import factory as F

KIND = {"detector": "機台異常", "supervisor": "主管指令", "command": "主管指令", "plant": "工廠事件", "event": "機台異常"}
STATE = {"detected": "偵測到", "classify": "分類", "plan": "擬計劃", "simulate": "模擬", "gate": "把關",
         "await_approval": "等主管核准", "execute": "執行中", "verify": "驗證中", "replan": "重新規劃",
         "closed": "已結案", "safe_stop": "安全停止"}
MAX_ROWS = 200


def _date(ts: Any) -> str | None:
    if not ts:
        return None
    t = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
    return t.astimezone(F.TZ).date().isoformat()


def _iso(ts: Any) -> str | None:
    return ts.isoformat() if isinstance(ts, datetime) else (str(ts) if ts else None)


def _mlabel(mid: str | None) -> str | None:
    if not mid or mid == "PLANT":
        return None
    from warroom.agent.narrate import machine_label
    try:
        return machine_label(mid)
    except Exception:  # noqa: BLE001 - an id the plant does not have keeps its code
        return mid


def _orders(text: str | None) -> list[str]:
    from warroom.agent.ask import orders_in
    return orders_in(text or "")


# query_plant topics that list every task: the 總覽 / 新對話 session's, never a task session's (issue #195)
TASK_LIST_TOPICS = frozenset({"open_events", "tasks", "questionable", "task_search", "handled_today"})


def scope_context(context: dict[str, Any], event_id: str) -> dict[str, Any]:
    """The chat context of one task's session: its own open event only, no plant overview, no other task (issue #195:
    「這個排程目前有兩件待核准的事件…」 in a task session, live run 10-22)."""
    out = {k: v for k, v in context.items() if k not in ("today", "overview", "today_index", "earlier_days")}
    out["open_events"] = [e for e in context.get("open_events") or [] if isinstance(e, dict) and e.get("id") == event_id]
    return out


def session_label(day: str | None, no: int | None) -> dict[str, str | None]:
    """「Nemo-1022-#01」 (full: Agent page, tooltip, search) and 「Nemo#01 · 10/22」 (task card, chat header). #00 is the
    新對話 / 總覽 session of that day."""
    if not day or no is None:
        return {"session": None, "session_short": None}
    d = datetime.fromisoformat(day)
    return {"session": f"Nemo-{d:%m%d}-#{no:02d}", "session_short": f"Nemo#{no:02d} · {d:%m/%d}"}


def live_row(r: Any, e: Any, subs: dict[str, Any]) -> dict[str, Any]:
    from warroom.api import overview
    st = e.state.value
    sa = subs.get(e.id) or {}
    working = st == "closed" and sa.get("status") == "waiting_tasks"
    what = overview._what(e)
    return {"id": e.id, "title": overview.label(e), "what": what, "kind": e.source, "kind_label": KIND.get(e.source, "任務"),
            "machine_id": None if e.machine_id == "PLANT" else e.machine_id, "machine_label": _mlabel(e.machine_id),
            "orders": _orders(f"{what} {e.goal or ''}"), "state": st,
            "state_label": "已結案，等現場完成" if working else STATE.get(st, st),
            "opened_at": _iso(e.opened_at), "closed_at": _iso(e.closed_at), "outcome": (e.outcome or "")[:160] or None,
            "live": True, "running": st not in ("closed", "safe_stop") or working,
            **session_label(*(_no(r, e.id) or (None, None)))}


def _no(r: Any, event_id: str) -> tuple[str, int] | None:
    tl = getattr(r, "tasklog", None)
    try:
        return tl.session_no(event_id) if tl is not None else None
    except Exception:  # noqa: BLE001 - a number is a label; the list never fails for it
        return None


def _log_row(x: dict[str, Any], live_ids: set[str]) -> dict[str, Any]:
    orders = [x["order_id"]] if x.get("order_id") else _orders(x.get("title"))
    src = x.get("source") or "event"
    kind = "supervisor" if src == "command" else "plant" if x.get("machine_id") == "PLANT" else "detector"
    title = x.get("title") or x["id"]
    who = KIND[kind] if kind != "detector" else (_mlabel(x.get("machine_id")) or "任務")
    # the factory day it belongs to: the day it closed; in the 一日循環 the date the cycle plays (a task the 24:00 wrap
    # cancelled closes at 00:00 of the next date)
    day = _date(x.get("opened_at")) if x.get("scenario") == "day_loop" else _date(x.get("closed_at"))
    return {"id": x["id"], "title": title if title.startswith(who) else f"{who}・{title[:24]}", "what": title,
            "kind": kind, "kind_label": KIND.get(kind, "任務"),
            "machine_id": None if x.get("machine_id") == "PLANT" else x.get("machine_id"),
            "machine_label": _mlabel(x.get("machine_id")), "orders": orders,
            "state": x.get("state"), "state_label": x.get("final") or STATE.get(str(x.get("state")), ""),
            "opened_at": x.get("opened_at"), "closed_at": x.get("closed_at"), "outcome": x.get("outcome"),
            "live": x["id"] in live_ids, "running": False, "qa_demo": bool(x.get("qa_demo")),
            "day": day, "loop": x.get("closed_loop"), "epoch": x.get("closed_epoch"),
            "scenario": x.get("scenario"), "seq": x.get("last_seq", 0), **session_label(x.get("date"), x.get("no"))}


def matches(row: dict[str, Any], q: str | None) -> bool:
    """Search: title, event id, machine (code or 7 號機), order id, type, state, outcome — case and spaces ignored."""
    if not q or not q.strip():
        return True
    hay = " ".join(str(v) for v in (row.get("id"), row.get("session"), row.get("session_short"), row.get("title"), row.get("what"), row.get("machine_id"),
                                     row.get("machine_label"), " ".join(row.get("orders") or []), row.get("kind_label"),
                                     row.get("state_label"), row.get("outcome")) if v)
    norm = lambda s: s.replace(" ", "").replace("#", "").upper()        # noqa: E731
    h = norm(hay)
    return all(norm(w) in h for w in q.split())


def day_label(day: str | None, loop: int | None) -> str:
    if not day:
        return "日期不明"
    d = datetime.fromisoformat(day)
    return f"{d:%m/%d}" + (f"（循環 {loop}）" if loop else "")


def listing(r: Any, q: str | None = None) -> dict[str, Any]:
    """{today, running, closed_today, history: [{key, label, tasks}]} — `q` filters every group."""
    now = r.clock.now()
    today = now.astimezone(F.TZ).date().isoformat()
    subs = {x["id"]: x for x in (r.orch.subagents() if getattr(r, "orch", None) is not None else [])}
    events = list(r.events())
    live = [live_row(r, e, subs) for e in sorted(events, key=lambda e: e.opened_at, reverse=True)]
    running = [x for x in live if x["running"]]
    run_ids = {x["id"] for x in running}
    tl = r.tasklog
    period = tl.period()
    closed_today: list[dict[str, Any]] = []
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for x in reversed(tl.rows()):
        if not x.get("closed_at"):
            continue
        row = _log_row(x, {e.id for e in events})
        if row["id"] in run_ids and row["live"] and row["epoch"] == period["epoch"]:
            continue                                     # still at work: listed under 進行中
        if row["epoch"] == period["epoch"] and row["loop"] == period["loop"] and row["day"] == today:
            closed_today.append(row)
            continue
        key = (row["day"], row["loop"], row["epoch"])
        g = groups.setdefault(key, {"key": f"{row['day']}|{row['loop'] or 0}|{row['epoch']}",
                                    # tasklog's loop is None before the first wrap: the day loop's 循環 1
                                    "label": day_label(row["day"], row["loop"] or (1 if row["scenario"] == "day_loop"
                                                                                   else None)),
                                    "scenario": row["scenario"],
                                    "seq": row["seq"], "tasks": []})
        g["seq"] = max(g["seq"], row["seq"])
        g["tasks"].append(row)
    history = sorted(groups.values(), key=lambda g: g["seq"], reverse=True)
    seen: dict[str, int] = {}
    for g in history:                                    # the same date + cycle again after a restart / reload
        n = seen.get(g["label"], 0)
        seen[g["label"]] = n + 1
        if n:
            g["label"] += f"・較早第 {n + 1} 次"
    out_hist = []
    for g in history:
        tasks = [t for t in g["tasks"] if matches(t, q)][:MAX_ROWS]
        if tasks:
            out_hist.append({"key": g["key"], "label": g["label"], "scenario": g["scenario"], "count": len(tasks),
                             "tasks": tasks})
    return {"today": {"date": today, "label": day_label(today, (r.day_loop_info() or {}).get("loop")
                                                        if hasattr(r, "day_loop_info") else None)},
            "general_session": session_label(today, 0),
            "q": q or "", "running": [x for x in running if matches(x, q)],
            "closed_today": [x for x in closed_today if matches(x, q)][:MAX_ROWS], "history": out_hist}


def session(r: Any, event_id: str) -> dict[str, Any]:
    """A task's session from the chat log on disk: its turns (oldest first) and the task row, read-only."""
    turns = r.history_log.chats(event_id=event_id, limit=400)
    lst = listing(r)
    row = next((t for t in [*lst["running"], *lst["closed_today"], *(t for g in lst["history"] for t in g["tasks"])]
                if t["id"] == event_id), None)
    return {"task": row, "turns": turns}
