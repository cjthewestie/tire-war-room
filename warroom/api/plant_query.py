"""query_plant: Agent Nemo's one general read over what the plant already knows (CJ, 2026-10-21: 「他的來源好像只來自我的
資料庫跟sop」「這樣是不是太限縮資料來源」). Read everything, write nothing.

Each topic is built from the Runtime's own read models (World snapshot, the active schedule, labor view, events, the
daylog, the audit log): the same data the pages show, no logic of its own. Each answer is compact JSON under
MAX_CHARS; a bigger one keeps fewer list items and says so. It is not one of the 9 ToolNames: the chat agent calls it,
app.py audits it as an R0 read (tool_call / tool_result rows, actor "tool:query_plant"); no plan or gate can call it."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from warroom import factory as F

MAX_CHARS = 2400          # about 1,500 tokens of mixed Chinese / JSON

STATUS = {"running": "運轉", "idle": "待機", "warning": "注意", "alarm": "異常", "stopped": "停機",
          "out_of_service": "停用", "offline": "離線"}
STATE = {"detected": "偵測到", "classify": "分類", "plan": "擬計劃", "simulate": "模擬", "gate": "把關",
         "await_approval": "等主管核准", "execute": "執行中", "verify": "驗證中", "replan": "重新規劃",
         "closed": "已結案", "safe_stop": "安全停止"}


def _hm(t: Any) -> str | None:
    if t is None:
        return None
    t = t if isinstance(t, datetime) else datetime.fromisoformat(str(t))
    return t.astimezone(F.TZ).strftime("%m/%d %H:%M")


def _world(r: Any) -> Any:
    try:
        return r.world_snapshot()
    except Exception:  # noqa: BLE001 - a sim without a World: the topic says so
        return None


def _open_events(r: Any) -> list[Any]:
    return [e for e in r.events() if e.closed_at is None]


def _dispatch_slots(now: datetime, days: int = 2) -> dict[str, Any]:
    """Outbound truck slots today and tomorrow (warroom/sim/world.py DISPATCH_TIMES on DISPATCH_DAYS)."""
    from warroom.sim.world import DISPATCH_DAYS, DISPATCH_TIMES
    out: dict[str, Any] = {}
    for k in range(days):
        d: date = (now + timedelta(days=k)).date()
        key = f"{'今天' if k == 0 else '明天'} {d:%m/%d}（{'一二三四五六日'[d.weekday()]}）"
        out[key] = [f"{t:%H:%M}" for t in DISPATCH_TIMES] if d.weekday() in DISPATCH_DAYS else "不出貨"
    return out


# ---------------------------------------------------------------------------------- topics
def overview(r: Any) -> dict[str, Any]:
    """今日工廠概況 as the 主控台 shows it (web/src/agent/overviewModel.ts): machines, next shipment, headcount,
    progress, open events."""
    tiles = r.machine_tiles()
    press = [t for t in tiles if t.machine_id.startswith("CURE-")]
    counts: dict[str, int] = {}
    for t in press:
        counts[STATUS.get(t.status.value, t.status.value)] = counts.get(STATUS.get(t.status.value, t.status.value), 0) + 1
    bad = [t.machine_id for t in tiles if t.status.value not in ("running", "idle")]
    w = _world(r)
    out: dict[str, Any] = {"factory_time": _hm(r.clock.now()), "presses": counts, "other_machines": len(tiles) - len(press),
                           "abnormal": bad, "open_events": len(_open_events(r))}
    if w is not None:
        out.update(next_shipment=_hm(w.dock.next_departure), waiting_ship_tyres=w.finished_goods.waiting_ship,
                   on_duty=w.people.on_duty, headcount=w.people.total,
                   orders_count_by_status=w.orders.pipeline)      # 張 (orders), not tyres or trucks
    return out


def shipments(r: Any) -> dict[str, Any]:
    w = _world(r)
    if w is None:
        return {"error": "這個模擬沒有出貨資料"}
    ev = [e for e in w.events if e.kind.startswith("shipment") or e.kind == "truck_arrive"]
    return {"factory_time": _hm(r.clock.now()), "next_departure": _hm(w.dock.next_departure),
            "dispatch_slots": _dispatch_slots(r.clock.now()),
            "rule": ("出貨車每個出貨時段（週一到週六 10:00、16:00）最多一班；那時有成品待出貨才會來，提前 45 分鐘到站裝貨。"
                     "待出貨的單位是條（輪胎），不是車"),
            "waiting_ship_tyres": w.finished_goods.waiting_ship, "finished_goods_tyres": w.finished_goods.by_size,
            "trucks_at_dock": [{"plate": t.plate, "kind": t.kind, "cargo": t.cargo, "leaves": _hm(t.leaves_at)}
                               for t in w.dock.trucks],
            "next_inbound": _hm(w.dock.next_arrival),
            "recent": [{"at": _hm(e.ts), "text": e.text} for e in ev[-6:]],
            "dock_log": [{"at": _hm(d.ts), "text": d.text} for d in w.dock.log[:6]]}


def schedule(r: Any) -> dict[str, Any]:
    store = r.c.store
    if store is None:
        return {"error": "排程資料庫沒有開"}
    v = store.current()
    now = r.clock.now()
    jobs = sorted(v.schedule.jobs, key=lambda j: j.mold_in)
    day = now.astimezone(F.TZ).date()
    demold = [f"{j.press_id} {j.order_id} {_hm(j.demold)[-5:]}" for j in sorted(jobs, key=lambda j: j.demold)
              if j.demold.astimezone(F.TZ).date() == day]
    mold_in = [f"{j.press_id} {j.order_id} {_hm(j.mold_in)[-5:]}" for j in jobs if j.mold_in.astimezone(F.TZ).date() == day]
    nxt = sorted((j for j in jobs if j.demold > now), key=lambda j: j.demold)[:3]
    # today's de-molds and mold-ins, not every press's queue: that made a 400-character reply cut off mid-line
    return {"version": v.version, "status": v.status.value if hasattr(v.status, "value") else v.status,
            "since": _hm(v.created_at), "reason": v.reason, "jobs_total": len(jobs),
            "demold_today": {"count": len(demold), "items": demold[:8]},
            "mold_in_today": {"count": len(mold_in), "items": mold_in[:8]},
            "next_demolds": [f"{j.press_id} {j.order_id} {_hm(j.demold)}" for j in nxt],
            "staffing_changes": [getattr(c, "label", None) or c.model_dump(mode="json") for c in v.schedule.staffing_changes][:5],
            "warnings": list(v.schedule.warnings)[:5]}


def materials(r: Any) -> dict[str, Any]:
    w = _world(r)
    if w is None:
        return {"error": "這個模擬沒有原料資料"}
    mats = list(w.materials)
    pos = [p for p in w.purchase_orders if p.status in ("在途", "延遲", "卸貨中")]
    # in transit from both sides: a purchase order on its way, or stock the World counts as on order without one
    # (「缺料怎麼辦」 → 「在途採購：沒有進行中」 while 尼龍布 was on order, and 「12 種原料」 for 6: rag-fix, PR #122)
    transit = [{"name": p.name, "qty": p.qty, "unit": p.unit, "eta": _hm(p.eta), "status": p.status} for p in pos]
    named = {p.name for p in pos}
    transit += [{"name": m.name, "qty": m.on_order, "unit": m.unit, "eta": None, "status": "已下單"}
                for m in mats if m.on_order > 0 and m.name not in named]
    return {"material_count": len(mats),
            "materials": [{"name": m.name, "stock": round(m.stock, 1), "unit": m.unit, "days_left": m.days_left,
                           "reorder_point": m.reorder_point, "on_order": m.on_order, "status": m.status} for m in mats],
            "status_meaning": {"ok": "足夠", "reorder": "該補貨", "low": "偏低", "out": "缺料"},
            "in_transit": transit or "目前沒有在途的採購"}


def open_events(r: Any) -> dict[str, Any]:
    pending = {a.event_id for a in r.approvals.pending()} if r.approvals is not None else set()
    items = [{"id": e.id, "machine": e.machine_id, "state": STATE.get(e.state.value, e.state.value), "opened": _hm(e.opened_at),
              "what": e.command or (e.alert_card.title if e.alert_card else e.anomaly.message), "goal": e.goal,
              "waiting_approval": e.id in pending} for e in sorted(_open_events(r), key=lambda e: e.opened_at)]
    return {"count": len(items), "events": items}


def handled_today(r: Any) -> dict[str, Any]:
    """The same durable list the Agent page shows (warroom/api/tasklog.py): chat and page agree."""
    day = r.tasklog.today()
    rows = [x for g in day["groups"] for x in g["rows"]]
    finals: dict[str, int] = {}
    for x in rows:
        finals[x["final"]] = finals.get(x["final"], 0) + 1
    return {"day": day["date"], "total": len(rows), "by_final": finals,
            "items": [{"id": x["id"], "opened": _hm(x["opened_at"]), "closed": _hm(x["closed_at"]),
                       "what": (x["title"] or "")[:40], "machine": x["machine_id"], "order": x["order_id"],
                       "final": x["final"], "loop": x["loop"], "scenario": x["scenario"],
                       "approvals": [f"v{a['version']} {a['decision']}" for a in x["approvals"]][-2:]} for x in rows][-12:]}


def audit_recent(r: Any) -> dict[str, Any]:
    rows = []
    for e in r.audit.tail(0, 1_000_000)[-40:]:
        p = e.payload or {}
        if e.kind.value == "system" and p.get("orch") == "queue":
            continue                                               # model-queue waits are noise here
        what = p.get("text") or p.get("msg") or p.get("decision") or p.get("action") or p.get("reply")
        rows.append({"at": _hm(e.ts), "kind": e.kind.value, "actor": e.actor, "event": e.event_id,
                     "what": str(what)[:60] if what else None})
    return {"entries": rows[-15:]}


def staffing(r: Any) -> dict[str, Any]:
    lv = r.labor_view()
    w = _world(r)
    now = r.clock.now()
    out: dict[str, Any] = {"shifts": [{"name": s["name"], "team": s["team_label"], "start": _hm(s["start"]), "end": _hm(s["end"]),
                                       "headcount": s["headcount"], "required": s["required"]} for s in lv["load"]
                                      if datetime.fromisoformat(s["end"]) > now][:12],      # current and coming shifts
                           "gaps": lv["gaps"]}
    if w is not None:
        out.update(on_duty=w.people.on_duty, total=w.people.total, by_state=w.people.by_state)
    return out


def orders(r: Any) -> dict[str, Any]:
    """The order lines #99 computes in code (warroom/tools/orders_today.py: progress, press, ETA, lateness), for the
    model to phrase; plus the World's pipeline."""
    w = _world(r)
    try:
        from warroom.tools import orders_today as OT
        data = OT.collect(r.sim, r.c.store, r.clock.now())
        today = {"overview": OT.overview_text(data, limit=5), "risk": OT.risk_overview_text(data, limit=4)}
    except Exception as e:  # noqa: BLE001 - the pipeline below still answers
        today = {"error": f"{type(e).__name__}: {e}"[:120]}
    if w is not None:
        return {"pipeline": w.orders.pipeline, "new_today": w.orders.new_today, "rush_open": w.orders.rush_open, **today}
    return today


def _orders_world(r: Any) -> dict[str, Any]:
    w = _world(r)
    if w is None:
        return {"orders": [o.model_dump(mode="json") for o in r.sim.orders()][:20]}
    return {"pipeline": w.orders.pipeline, "new_today": w.orders.new_today, "rush_open": w.orders.rush_open,
            "items": [{"id": o.id, "size": o.size.value, "qty": o.qty, "done": o.done, "due": o.due.isoformat(),
                       "customer": o.customer, "status": o.status, "rush": o.rush} for o in w.orders.items][:20]}


def machines(r: Any) -> dict[str, Any]:
    return {"machines": [{"id": t.machine_id, "status": STATUS.get(t.status.value, t.status.value), "temp_c": t.temp_c}
                         for t in r.machine_tiles()]}


def quality(r: Any) -> dict[str, Any]:
    w = _world(r)
    if w is None:
        return {"error": "這個模擬沒有品質資料"}
    q = w.quality
    return {"inspected_today": q.inspected_today, "defects_today": q.defects_today, "yield_pct": q.yield_pct,
            "recent": [{"at": _hm(i.ts), "machine": i.machine_id, "result": i.result, "cause": i.cause} for i in q.recent[:6]]}


def maintenance(r: Any) -> dict[str, Any]:
    w = _world(r)
    if w is None:
        return {"error": "這個模擬沒有保養資料"}
    m = w.maintenance

    def row(x: Any) -> dict[str, Any]:
        return {"machine": x.machine_id, "title": x.title, "start": _hm(x.start), "status": x.status}
    return {"in_progress": [row(x) for x in m.in_progress], "upcoming": [row(x) for x in m.upcoming[:6]],
            "recent": [row(x) for x in m.recent[:4]]}


def energy(r: Any) -> dict[str, Any]:
    w = _world(r)
    if w is None:
        return {"error": "這個模擬沒有用電資料"}
    e = w.energy
    return {"total_kw": round(e.total_kw, 1), "contract_kw": e.contract_kw, "peak": e.peak, "kwh_today": round(e.kwh_today, 1),
            "by_line": {k: round(v, 1) for k, v in e.by_line.items()}}


# ---------------------------------------------------------------------------------- past orders (CJ 2026-10-22:
# 「我有沒有地方可以詢問之前的訂單」): the World's order book (created / completed / shipped), its event feed, the imported
# orders DB when there is one, the team dataset's order plan and daily output, and the events that named the order.
def _world_obj(r: Any) -> Any:
    return getattr(r.sim, "world", None)


def _norm(oid: str) -> str:
    return str(oid).upper().replace("#", "").replace("-", "")


def _world_orders(r: Any) -> list[Any]:
    """Every order the World shows (seed and world orders, WorldSnapshot.orders), with completed_at / start_at from
    its own book where it keeps one."""
    from types import SimpleNamespace
    w, snap = _world_obj(r), _world(r)
    book = getattr(w, "_orders", {}) if w is not None else {}
    out = []
    for o in (snap.orders.items if snap is not None else []):
        mine = book.get(o.id)
        out.append(SimpleNamespace(**o.model_dump(), completed_at=getattr(mine, "completed_at", None),
                                   start_at=getattr(mine, "start_at", None)))
    return out


def _late(o: Any) -> bool | None:
    done = getattr(o, "shipped_at", None) or getattr(o, "completed_at", None)
    if done is None:
        return None
    return done.astimezone(F.TZ).date() > o.due


def _dataset(name: str) -> list[dict[str, str]]:
    try:
        from warroom.data.team_dataset import load_table
        return load_table(name)
    except Exception:  # noqa: BLE001 - no dataset in this checkout: that source is left out
        return []


def orders_history(r: Any) -> dict[str, Any]:
    done = [o for o in _world_orders(r) if o.status in ("完成", "待出貨", "已出貨") or getattr(o, "shipped_at", None)]
    done.sort(key=lambda o: getattr(o, "shipped_at", None) or getattr(o, "completed_at", None) or r.clock.now(), reverse=True)
    items = [{"id": o.id, "customer": o.customer, "qty": o.qty, "due": o.due.isoformat(), "status": o.status,
              "completed": _hm(getattr(o, "completed_at", None)), "shipped": _hm(getattr(o, "shipped_at", None)),
              "late": _late(o)} for o in done]
    w = _world_obj(r)
    shipped = [{"at": _hm(e.ts), "text": e.text, "on_time": e.on_time} for e in list(getattr(w, "events", []))
               if e.kind in ("shipment_departed", "order_completed")][-8:]
    plan = [{"id": x.get("order_id"), "customer": x.get("customer"), "qty": x.get("qty"), "due": x.get("due"),
             "completion": x.get("completion"), "late": (x.get("completion") or "") > (x.get("due") or "~")}
            for x in _dataset("orders")]
    daily = [{"date": x.get("date"), "tires_done": x.get("tires_done"), "downtime_min": x.get("downtime_min")}
             for x in _dataset("history_daily")][-7:]
    return {"completed_or_shipped": items[:12], "late": [i["id"] for i in items if i["late"]],
            "recent_feed": shipped,
            # the dataset plan as a count and its late ids only: the full list made the answer slow and long
            "team_dataset_plan": {"note": "團隊資料集的訂單計畫（原始日期）", "orders": len(plan),
                                  "late": [p["id"] for p in plan if p["late"]]},
            "daily_output": daily[-3:]}


def order_lookup(r: Any, order_id: str | None = None) -> dict[str, Any]:
    if not order_id:
        return {"error": "要給 order_id"}
    key = _norm(order_id)
    out: dict[str, Any] = {"order_id": order_id}
    o = next((x for x in _world_orders(r) if _norm(x.id) == key), None)
    if o is not None:
        out["world"] = {"customer": o.customer, "qty": o.qty, "done": o.done, "due": o.due.isoformat(), "status": o.status,
                        "rush": o.rush, "created": _hm(o.created_at), "started": _hm(getattr(o, "start_at", None)),
                        "completed": _hm(getattr(o, "completed_at", None)), "shipped": _hm(getattr(o, "shipped_at", None)),
                        "late": _late(o)}
    w = _world_obj(r)
    feed = [{"at": _hm(e.ts), "text": e.text} for e in list(getattr(w, "events", []))
            if _norm(e.ref or "") == key or key in {_norm(x) for x in (e.order_ids or [])}]
    if feed:
        out["feed"] = feed[-8:]
    db = getattr(r, "orders_db", None)
    if db is not None:
        try:
            row = next((x for x in db.orders([order_id])), None)
            if row is not None:
                out["orders_db"] = {"qty": row.qty, "done": row.done, "due": row.due.isoformat(), "customer": row.customer}
        except Exception:  # noqa: BLE001, S110 - the other sources still answer
            pass
    store = r.c.store
    if store is not None:
        try:
            jobs = [j for j in store.current().schedule.jobs if _norm(j.order_id) == key]
            if jobs:
                out["schedule"] = {"jobs": len(jobs), "presses": sorted({j.press_id for j in jobs}),
                                   "first_mold_in": _hm(min(j.mold_in for j in jobs)), "last_demold": _hm(max(j.demold for j in jobs))}
        except Exception:  # noqa: BLE001, S110
            pass
    ds = next((x for x in _dataset("orders") if _norm(x.get("order_id", "")) == key), None)
    units = [x for x in _dataset("tire_unit") if _norm(x.get("order_id", "")) == key or _norm(x.get("alias", "")) == key]
    if ds or units:
        out["team_dataset"] = {"note": "團隊資料集（原始日期）", "order": ds,
                               "tyres": len(units), "cure_first": min((u["cure_start"] for u in units), default=None),
                               "cure_last": max((u["cure_end"] for u in units), default=None)}
    evs = [e for e in r.events() if order_id in (e.command or "") or order_id in (e.alert_card.title if e.alert_card else "")
           or order_id in (e.anomaly.message or "")]
    if evs:
        out["events"] = [{"id": e.id, "opened": _hm(e.opened_at), "state": STATE.get(e.state.value, e.state.value),
                          "what": (e.command or (e.alert_card.title if e.alert_card else e.anomaly.message))[:40]} for e in evs][-6:]
    if len(out) == 1:
        out["note"] = "沒有找到這張訂單的任何紀錄"
    return out


def tasks(r: Any) -> dict[str, Any]:
    """現在所有任務狀況: every open event and pre-plan, in plain states, with what it waits on and for how long."""
    now = r.clock.now()
    subs = {x["id"]: x for x in r.orch.subagents()}
    pending = {a.event_id: a for a in r.approvals.pending()} if r.approvals is not None else {}
    pre = getattr(r.orch, "preplan_ids", set())
    items = []
    for e in sorted(_open_events(r), key=lambda e: e.opened_at):
        sa = subs.get(e.id, {})
        card = pending.get(e.id)
        st = e.state.value
        if card is not None:
            label, waits = f"待核准 v{card.version}", "等主管核准"
        elif st in ("execute", "verify") and sa.get("tasks_total"):
            label = f"執行中 {sa['tasks_done']}/{sa['tasks_total']}" if st == "execute" else "驗證中"
            waits = "等現場完成" if st == "execute" else "等讀值驗證"
        elif st == "verify":
            label, waits = "驗證中", "等讀值驗證"
        elif st == "execute":
            label, waits = "執行中", "執行核准的步驟"
        elif st in ("detected", "classify", "plan", "simulate", "gate", "replan"):
            label, waits = "規劃中", "等 AI 擬計劃" + ("（模型排隊中）" if sa.get("status") == "waiting_model" else "")
        else:
            label, waits = STATE.get(st, st), ""
        last = e.timeline[-1].ts if e.timeline else e.opened_at
        items.append({"id": e.id, "what": (e.command or (e.alert_card.title if e.alert_card else e.anomaly.message))[:40],
                      "machine": e.machine_id, "state": label, "waits_on": waits, "preplan": e.id in pre,
                      "open_min": round((now - e.opened_at).total_seconds() / 60, 1),
                      "in_state_min": round((now - last).total_seconds() / 60, 1), "round": len(e.prior_rounds) + 1})
    return {"count": len(items), "tasks": items}


def safety_stops(r: Any) -> dict[str, Any]:
    """Safety Core stops and their way back: ① 派現場檢查 ② 讀值回安全範圍 ③ 主管放行 (the button, or the chat after the
    supervisor's 好 since CJ 2026-10-22 「開放」; ① and ② are checked either way)."""
    board = getattr(getattr(r.sim, "world", None), "tasks", None)
    try:
        cases = r.cases.view(board)
    except Exception:  # noqa: BLE001 - no Safety Core cases in this sim
        cases = []
    items = []
    for c in cases:
        if c.status != "open":
            continue
        items.append({"machine": c.machine_id, "what": c.message[:40], "since": _hm(c.ts),
                      "①派現場檢查": f"{c.tasks_done}/{len(c.task_ids)} 完成" if c.task_ids else "還沒派",
                      "②讀值回安全範圍": f"{c.safe_samples}/{c.safe_needed}" + ("（已達標）" if c.reading_ok else ""),
                      "③主管放行": "主管在主控台按「確認恢復運轉」，或在對話說「恢復這台」再回「好」；①②沒過不能恢復"})
    return {"count": len(items), "stops": items}


def questionable(r: Any) -> dict[str, Any]:
    """有疑問的任務 (warroom/api/overview.py): open tasks the code flags, each with its reasons."""
    from warroom.api.overview import questionable as q
    items = q(r)
    return {"count": len(items), "questionable": items}


# ---------------------------------------------------------------------------------- memory (CJ 2026-10-22: 「接記憶」)
def machine_history(r: Any, machine_id: str | None = None) -> dict[str, Any]:
    """One machine's 後台 notes and its last past events: the same block the commander gets (context.machine_memory)."""
    if not machine_id:
        return {"error": "要給 machine_id"}
    if machine_id not in F.MACHINE_BY_ID:
        return {"error": f"沒有這台機台：{machine_id}"}
    from warroom.data import machine_memory as MM
    block = MM.for_machine(machine_id, getattr(r, "memory_notes", None), r.audit, r.events())
    return block or {"machine_id": machine_id, "notes": [], "past": [], "note": "這台機台沒有筆記，也沒有過去事件紀錄"}


def memory_notes(r: Any) -> dict[str, Any]:
    """Every note the supervisor wrote in 後台 記憶, newest first (「我在後台寫了什麼」)."""
    from warroom.agent.narrate import machine_label
    notes = getattr(r, "memory_notes", None)
    rows = sorted(notes.all(), key=lambda n: n.created_at, reverse=True) if notes is not None else []
    return {"count": len(rows), "notes": [{"machine": machine_label(n.machine_id), "machine_id": n.machine_id,
                                           "at": _hm(n.created_at), "by": n.author, "text": n.text} for n in rows]}


TOPICS = {"overview": overview, "shipments": shipments, "schedule": schedule, "materials": materials,
          "open_events": open_events, "handled_today": handled_today, "audit_recent": audit_recent,
          "staffing": staffing, "orders": orders, "machines": machines, "quality": quality,
          "maintenance": maintenance, "energy": energy, "orders_history": orders_history, "order_lookup": order_lookup, "tasks": tasks,
          "safety_stops": safety_stops, "questionable": questionable,
          "machine_history": machine_history, "memory_notes": memory_notes}


def _shrink(d: Any, n: int) -> Any:
    if isinstance(d, list):
        return [_shrink(x, n) for x in d[:n]]
    if isinstance(d, dict):
        return {k: _shrink(v, n) for k, v in d.items()}
    return d


def query(r: Any, topic: str, order_id: str | None = None, machine_id: str | None = None) -> dict[str, Any]:
    """{topic: data} for one topic, at most MAX_CHARS as JSON; KeyError for an unknown topic."""
    if topic == "order_lookup":
        data = TOPICS[topic](r, order_id)
    elif topic == "machine_history":
        data = TOPICS[topic](r, machine_id)
    else:
        data = TOPICS[topic](r)
    n = 20
    while len(json.dumps(data, ensure_ascii=False, default=str)) > MAX_CHARS and n > 1:
        n //= 2
        data = {**_shrink(data, n), "note": f"資料太多，每個清單只留前 {n} 筆"}
    text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text) > MAX_CHARS:
        return {"text": text[:MAX_CHARS], "note": "資料太多，已截斷"}
    return data
