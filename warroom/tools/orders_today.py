"""今日訂單 (issue #95): the ONE place that decides which orders are in production today, which are due today,
how far each one is and whether it is late or at risk. The Agent (問戰情室 through query_orders_shifts), the
GET /api/orders/today endpoint behind the 訂單 overview on desktop and phone, and the tests all read this.

Read only, no model: every number is counted or derived here from the plant's own data, and a number that cannot be
derived is None (shown as 待確認 / 尚無排程), never 0 and never a guess.

Inputs (all existing data, nothing made up for this feature)
- orders: the dashboard's order list = the factory's order book (seed, from the team dataset) plus the orders that
  arrived in the world (#W-nnnn), each id once (World.order_inputs). Without a world, sim.orders().
- jobs: the ACTIVE schedule's jobs (store.current()). A PROPOSED version waiting for approval is not in force and
  is never read here.
- cures: what each curing press holds right now (sim machine cure_job): the tyres that are in a press today.
- warnings: the ACTIVE schedule's own capacity warnings ("... 預估 10/16 ..."), the only source of an estimate for
  tyres the schedule window does not hold.

Rules
- 「今天」 is the factory clock's date (the sim clock in a demo), Asia/Taipei.
- 今日生產 = a tyre of the order is in a press now (including one that started on an earlier day and is still
  curing), or the ACTIVE schedule has a job whose process window (A-line feed .. de-mold) overlaps today, or a
  world order is in production or completed today (a world order only planned to start later is not in production). 今日交貨 = the due DATE is today. The two are
  separate lists; an order can be in both, and is counted once in `unique_today`.
- Due: when the source states a time (the team dataset does) lateness is judged on that exact time and may be said in
  hours. When it states only a date, lateness is judged on the DATE (late = after the due day) and no hour count is
  ever claimed. This is the same on every path, and the same rule the 訂單 tab uses.
- 預計完成 (eta) is the last scheduled de-mold of the order's tyres, and ONLY when the schedule (tyres in a press plus
  scheduled jobs) covers every tyre still to make. Otherwise eta is None: the last scheduled tyre's de-mold is never
  passed off as the whole order's finish. The next single tyre's de-mold is `next_demold`, a separate field.
- A world order has no schedule: it shows 尚無排程, with no machine and no finish time.
- Risk: done / complete as the 訂單 tab; `late` when the order is past its due, its eta is past its due or the
  schedule's own estimate is past its due; `risk` for the other reasons the 訂單 tab lists; else `ok`.
  `overdue` (already past due and not finished) is kept apart from `late` (will be late)."""
from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta
from typing import Any

from warroom import factory as F

UNKNOWN = "待確認"
NO_SCHEDULE = "尚無排程"
SIZE_LABEL = {"S": "小胎", "M": "中胎", "L": "大胎"}
RISK_RANK = {"late": 0, "risk": 1, "ok": 2, "complete": 3, "done": 4}
RISK_TEXT = {"late": "有遲交風險", "risk": "有風險", "ok": "目前未見遲交風險", "complete": "已完成", "done": "已出貨"}
SOON_H = 72                      # an order with tyres outside the schedule is a risk when due within this many hours
_ID_RX = re.compile(r"(?<![A-Za-z])#?[A-Za-z]{1,2}-\d{1,4}\b")


def norm_id(s: str) -> str:
    return str(s).strip().lstrip("#").upper()


def ids_in_text(text: str) -> list[str]:
    """Order-id-shaped words in a question, normalised ('#A-311', 'a-311' -> 'A-311'), in order, once each. Whether
    they are real orders is decided against the order list, not here (a machine id such as CURE-07 never matches)."""
    out: list[str] = []
    for m in _ID_RX.findall(text or ""):
        n = norm_id(m)
        if n not in out:
            out.append(n)
    return out


def _size(v: Any) -> str:
    return str(getattr(v, "value", v))


def _end_of_day(d: date) -> datetime:
    return datetime.combine(d, time(23, 59), tzinfo=F.TZ)


def _press_label(pid: str) -> str:
    try:
        return f"{int(pid.split('-')[1])} 號機"
    except (IndexError, ValueError):
        return pid


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t is not None else None


def _hm(t: datetime) -> str:
    return f"{t:%m/%d %H:%M}"


def _schedule_note(order_id: str, warnings: list[str]) -> tuple[str | None, str | None]:
    """The part of the ACTIVE schedule's warnings about one order, and the 'M/D' it estimates (if it names one)."""
    note = proj = None
    for w in warnings:
        if order_id not in w:
            continue
        part = next((x for x in re.split(r"[：、；]", w) if order_id in x), w).strip()
        note = note or part
        m = re.search(r"預估\s*(\d{1,2})/(\d{1,2})", part)
        proj = proj or (f"{int(m.group(1))}/{int(m.group(2))}" if m else None)
    return note, proj


def _proj_date(proj: str | None, due: date) -> date | None:
    if not proj:
        return None
    m, d = (int(x) for x in proj.split("/"))
    try:
        cand = date(due.year, m, d)
    except ValueError:
        return None
    return date(due.year + 1, m, d) if cand < due - timedelta(days=180) else cand


# ------------------------------------------------------------------ one order
def _brief(o: dict[str, Any], now: datetime, today: date, jobs: list[Any], cures: list[dict[str, Any]],
           warnings: list[str]) -> dict[str, Any]:
    oid, qty, done = o["id"], int(o["qty"]), int(o["done"])
    status = o["status"]
    shipped = status == "已出貨"
    finished = shipped or status in ("完成", "待出貨") or done >= qty
    remaining = max(0, qty - done)
    due_d: date = o["due"]
    due_at: datetime | None = o.get("due_at")
    timed = due_at is not None
    due_end = due_at if timed else _end_of_day(due_d)

    future = sorted((j for j in jobs if j.mold_in > now), key=lambda j: j.mold_in)
    in_press = sorted(cures, key=lambda c: c["planned_demold_at"])
    covered = len(in_press) + len(future)
    unscheduled = None if finished else max(0, remaining - covered)
    demolds = [c["planned_demold_at"] for c in in_press] + [j.demold for j in future]
    next_demold = min(demolds) if demolds and not finished else None
    eta = max(demolds) if demolds and unscheduled == 0 and not finished else None
    machines = list(dict.fromkeys([c["press_id"] for c in in_press] + [j.press_id for j in future]))
    machines_now = list(dict.fromkeys(c["press_id"] for c in in_press))

    note, proj = _schedule_note(oid, warnings)
    proj_d = _proj_date(proj, due_d)
    overdue = (not finished) and ((now > due_at) if timed else (now.date() > due_d))
    if eta is not None:
        late = eta > due_at if timed else eta.date() > due_d
    else:
        late = False
    proj_late = proj_d is not None and proj_d > due_d

    reasons: list[str] = []        # why the order is late / at risk (each one traceable to a number above)
    if overdue:
        reasons.append(f"已超過交期 {_hm(due_at)}" if timed else f"已超過交期日 {due_d:%m/%d}")
    if late and eta is not None:
        if timed:
            reasons.append(f"預估完成 {_hm(eta)}，比交期晚 {math.ceil((eta - due_at).total_seconds() / 3600)} 小時")
        else:
            reasons.append(f"預估完成 {_hm(eta)}，晚於交期日 {due_d:%m/%d}（{(eta.date() - due_d).days} 天）")
    if note and not finished:
        reasons.append("排程視窗外預估：" + note.replace(oid, "").strip(" ，,"))
    never_scheduled = (not jobs) and (not cures) and (not finished) and status == "待排程"
    if never_scheduled:
        reasons.append("還沒排進硫化排程")
    elif unscheduled and (due_end - now) < timedelta(hours=SOON_H):
        reasons.append(f"還有 {unscheduled} 條沒有排程依據，交期 3 天內")
    risk = ("done" if shipped else "complete" if finished else
            "late" if (overdue or late or proj_late) else "risk" if reasons else "ok")

    # today
    t0 = datetime.combine(today, time.min, tzinfo=F.TZ)
    t1 = t0 + timedelta(days=1)
    today_jobs = [j for j in jobs if j.a_feed_start < t1 and j.demold > t0]
    basis: list[str] = []
    if in_press:
        basis.append("硫化中：" + "、".join(_press_label(p) for p in machines_now))
    if today_jobs:
        basis.append(f"今日排程有 {len(today_jobs)} 條製程活動")
    if o["source"] == "world":
        if status == "生產中":
            basis.append("訂單生產中（世界模擬，無機台排程）")
        at = o.get("completed_at")
        if at is not None and t0 <= at < t1:
            basis.append(f"今日完成 {at:%H:%M}")
    in_production_today = bool(basis) and not (shipped and not in_press and not today_jobs)

    if finished:
        state = status
    elif in_press:
        state = f"生產中・{len(in_press)} 條硫化中"
    elif o["source"] == "world":
        state = f"{status}・{NO_SCHEDULE}" if not jobs else status
    else:
        state = status
    return {
        "id": oid, "size": _size(o["size"]), "size_label": SIZE_LABEL.get(_size(o["size"]), _size(o["size"])),
        "customer": o.get("customer"), "source": o["source"], "rush": bool(o.get("rush")),
        "qty": qty, "done": done, "remaining": remaining,
        "status": status, "state_text": state,
        "machines": machines, "machines_now": machines_now,
        "has_schedule": bool(jobs or cures),
        "schedule_state": None if (jobs or cures) else (NO_SCHEDULE if not finished else None),
        "in_press": len(in_press), "scheduled_ahead": len(future),
        "unscheduled": unscheduled,
        "next_demold": _iso(next_demold),
        "eta": _iso(eta), "eta_basis": "ACTIVE 排程（全部剩餘數量都已排）" if eta is not None else None,
        "eta_unknown_reason": (None if (eta is not None or finished) else
                               (NO_SCHEDULE if covered == 0 else f"排程只涵蓋 {covered}／{remaining} 條")),
        "schedule_estimate": proj, "schedule_note": note,
        "due": due_d.isoformat(), "due_at": _iso(due_at), "due_basis": "time" if timed else "date",
        "overdue": overdue, "late_by_h": (math.ceil((eta - due_at).total_seconds() / 3600)
                                          if (late and timed and eta is not None) else None),
        "risk": risk, "risk_text": ("已遲交" if overdue and risk == "late" else RISK_TEXT[risk]),
        "reasons": reasons,
        "in_production_today": in_production_today, "production_basis": basis,
        "due_today": due_d == today,
    }


# ------------------------------------------------------------------ all orders
def build(now: datetime, orders: list[dict[str, Any]], jobs: list[Any], cures: list[dict[str, Any]],
          warnings: list[str] | None = None, schedule_version: int | None = None) -> dict[str, Any]:
    """The whole answer: every order's brief (risk first), the today lists and counts. `orders` are dicts with
    id, size, qty, done, due (date), due_at (datetime | None), customer, status, rush, source ('seed' | 'world')
    and optionally start_at / completed_at / shipped_at; `jobs` are the ACTIVE schedule's ScheduleJobs; `cures` are
    {press_id, order_id, planned_demold_at, ...} for the tyres in a press now."""
    warnings = list(warnings or [])
    today = now.astimezone(F.TZ).date() if now.tzinfo else now.date()
    by_order_jobs: dict[str, list[Any]] = {}
    for j in jobs:
        by_order_jobs.setdefault(norm_id(j.order_id), []).append(j)
    by_order_cures: dict[str, list[dict[str, Any]]] = {}
    for c in cures:
        by_order_cures.setdefault(norm_id(c["order_id"]), []).append(c)
    seen: set[str] = set()
    duplicates: list[str] = []
    briefs: list[dict[str, Any]] = []
    for o in orders:
        key = norm_id(o["id"])
        if key in seen:
            duplicates.append(o["id"])
            continue
        seen.add(key)
        briefs.append(_brief(o, now, today, by_order_jobs.get(key, []), by_order_cures.get(key, []), warnings))
    briefs.sort(key=lambda b: (RISK_RANK[b["risk"]], not b["overdue"], not b["rush"], b["due_at"] or b["due"], b["id"]))
    production = [b["id"] for b in briefs if b["in_production_today"]]
    delivery = [b["id"] for b in briefs if b["due_today"]]
    overdue = [b["id"] for b in briefs if b["overdue"] and b["risk"] == "late"]
    late = [b["id"] for b in briefs if b["risk"] == "late" and b["id"] not in overdue]     # will be late
    other_risk = [b["id"] for b in briefs if b["risk"] == "risk"]                           # e.g. not scheduled yet
    at_risk = late + other_risk
    unique_today = list(dict.fromkeys(production + delivery))
    return {
        "date": today.isoformat(), "as_of": now.isoformat(), "schedule_version": schedule_version,
        "orders": briefs, "duplicates": duplicates,
        "production": production, "delivery": delivery, "overdue": overdue, "late": late,
        "other_risk": other_risk, "at_risk": at_risk,
        "counts": {"total": len(briefs), "production": len(production), "delivery": len(delivery),
                   "overdue": len(overdue), "late": len(late), "other_risk": len(other_risk),
                   "at_risk": len(at_risk), "unique_today": len(unique_today)},
    }


def find(data: dict[str, Any], order_id: str) -> dict[str, Any] | None:
    key = norm_id(order_id)
    return next((b for b in data.get("orders") or [] if norm_id(b["id"]) == key), None)


# ------------------------------------------------------------------ inputs from the plant
def collect(sim: Any, store: Any, now: datetime) -> dict[str, Any]:
    """Build the brief from a live sim and the schedule store. Everything is read only; a missing piece is left out
    (no store -> no jobs, so orders show 尚無排程), never filled in."""
    cures: list[dict[str, Any]] = []
    for pid in F.CURE_PRESS_IDS:
        try:
            cj = sim.machine(pid).cure_job
        except Exception:  # noqa: BLE001 - a machine the sim cannot report is a gap, not a guess
            continue
        if cj is not None:
            cures.append({"press_id": pid, "order_id": cj.order_id, "mold_in_at": cj.mold_in_at,
                          "planned_demold_at": cj.planned_demold_at, "cure_elapsed_h": cj.cure_elapsed_h,
                          "cure_required_h": cj.cure_required_h})
    in_press_by_order: dict[str, int] = {}
    for c in cures:
        in_press_by_order[norm_id(c["order_id"])] = in_press_by_order.get(norm_id(c["order_id"]), 0) + 1
    world = getattr(sim, "world", None)
    if world is not None and callable(getattr(world, "order_inputs", None)):
        orders = world.order_inputs(now)
    else:
        due_at = getattr(sim, "due_at", None)
        orders = []
        for o in sim.orders():
            st = ("完成" if o.done >= o.qty else
                  "生產中" if o.done > 0 or in_press_by_order.get(norm_id(o.id)) else "待排程")
            orders.append({"id": o.id, "size": o.size, "qty": o.qty, "done": o.done, "due": o.due,
                           "due_at": due_at(o.id) if callable(due_at) else None, "customer": o.customer,
                           "status": st, "rush": o.priority >= 2, "source": "seed"})
    jobs: list[Any] = []
    warnings: list[str] = []
    version = None
    if store is not None:
        try:
            cur = store.current()                  # the ACTIVE version only; a PROPOSED one is not in force
            jobs, warnings, version = list(cur.schedule.jobs), list(cur.schedule.warnings), cur.version
        except Exception:  # noqa: BLE001 - empty store: no jobs
            pass
    return build(now, orders, jobs, cures, warnings, version)


def collect_from_db(db: Any, now: datetime) -> dict[str, Any]:
    """The same brief from the Excel-backed OrdersDB (no sim): no cures and no world orders, so an order is only
    as known as the workbook's schedule sheet makes it."""
    orders = []
    for o in db.orders():
        st = "完成" if o.done >= o.qty else "生產中" if o.done > 0 else "待排程"
        orders.append({"id": o.id, "size": o.size, "qty": o.qty, "done": o.done, "due": o.due, "due_at": None,
                       "customer": o.customer, "status": st, "rush": o.priority >= 2, "source": "seed"})
    return build(now, orders, list(db.jobs()), [], [], None)


# ------------------------------------------------------------------ text for the answer (code, not the model)
def _due_text(b: dict[str, Any]) -> str:
    if b["due_basis"] == "time" and b["due_at"]:
        return _hm(datetime.fromisoformat(b["due_at"]))
    return f"{datetime.fromisoformat(b['due'] + 'T00:00:00'):%m/%d}（僅日期）"


def order_line(b: dict[str, Any]) -> str:
    """One order in one line, only what the data holds; what it does not hold says so."""
    if b["machines"]:
        mach = "、".join(_press_label(p) for p in b["machines"])
    else:
        mach = NO_SCHEDULE if not b["has_schedule"] and b["status"] not in ("完成", "待出貨", "已出貨") else "無"
    if b["status"] in ("完成", "待出貨", "已出貨"):
        eta = "已完成"
    elif b["eta"]:
        eta = _hm(datetime.fromisoformat(b["eta"])) + "（排程預估）"
    else:
        eta = f"{UNKNOWN}（{b['eta_unknown_reason'] or '無依據'}）"
    bits = [b["id"], f"{b['size_label']}共 {b['qty']} 條", f"已完成 {b['done']}／剩 {b['remaining']}",
            f"狀態 {b['state_text']}", f"機台 {mach}", f"交期 {_due_text(b)}", f"預計完成 {eta}"]
    if b["next_demold"] and b["machines"] and b["status"] not in ("完成", "待出貨", "已出貨"):
        bits.append(f"下一條出模 {_hm(datetime.fromisoformat(b['next_demold']))}")
    bits.append(("風險：" + b["risk_text"] + ("（" + "；".join(b["reasons"]) + "）" if b["reasons"] else "")))
    return "｜".join(bits)


def overview_text(data: dict[str, Any], *, limit: int = 12) -> str:
    """今日 overview: production and delivery listed apart, each order once, risky ones first. The counts are of the
    whole result; only the lines are cut at `limit`."""
    c = data["counts"]
    by_id = {b["id"]: b for b in data["orders"]}
    d = datetime.fromisoformat(data["date"])
    head = (f"今天（{d:%m/%d}）：今日生產 {c['production']} 張、今日交貨 {c['delivery']} 張"
            f"（合計 {c['unique_today']} 張不重複）。全部 {c['total']} 張訂單中：已遲交 {c['overdue']} 張、"
            f"預估會遲交 {c['late']} 張、其他風險 {c['other_risk']} 張。")
    if not c["production"] and not c["delivery"]:
        return head + "沒有今日生產或今日交貨的訂單。"
    parts = [head]
    shown: set[str] = set()
    for title, ids in (("今日生產", data["production"]), ("今日交貨", data["delivery"])):
        if not ids:
            parts.append(f"{title}：沒有。")
            continue
        lines = []
        for i in ids[:limit]:
            lines.append(f"{i}（已列於今日生產）" if i in shown else order_line(by_id[i]))
            shown.add(i)
        more = f"…另有 {len(ids) - limit} 張，見訂單頁" if len(ids) > limit else ""
        parts.append(f"{title}：" + "；".join(lines) + more)
    return " ".join(parts)


def risk_overview_text(data: dict[str, Any], *, limit: int = 12) -> str:
    """哪些訂單有遲交風險: 已遲交 (already past due) and 預估會遲交 (will be late) apart, then the other risks
    (e.g. not scheduled yet), which are not a late-delivery finding and are said so."""
    by_id = {b["id"]: b for b in data["orders"]}
    c = data["counts"]
    if not (data["overdue"] or data["late"] or data["other_risk"]):
        return f"目前沒有已遲交、預估會遲交或有其他風險的訂單（共檢查 {c['total']} 張）。"
    parts = [f"共檢查 {c['total']} 張：已遲交 {c['overdue']} 張、預估會遲交 {c['late']} 張、"
             f"其他風險（例如尚未排程，不等於會遲交）{c['other_risk']} 張。"]
    for title, key in (("已遲交", "overdue"), ("預估會遲交", "late"), ("其他風險", "other_risk")):
        ids = data[key]
        if ids:
            more = f"…另有 {len(ids) - limit} 張，見訂單頁" if len(ids) > limit else ""
            parts.append(f"{title}：" + "；".join(order_line(by_id[i]) for i in ids[:limit]) + more)
    return " ".join(parts)
