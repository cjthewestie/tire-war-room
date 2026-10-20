"""Chinese labels and timeline sentences for the page-11 style event timeline.

Pure formatting: every number shown comes from a tool result (formula / solver / sensors), never
from the model. Formatters are defensive (``.get``) because tool payloads are dicts."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from warroom.contracts import Channel, Line, NotifyKind, PlanStep, ToolName
from warroom.factory import MACHINE_BY_ID, TEAM_LABELS

TOOL_LABELS: dict[ToolName, str] = {
    ToolName.READ_SENSORS: "讀感測器",
    ToolName.PREDICT_ANOMALY: "異常預測",
    ToolName.CURE_FORMULA: "硫化公式",
    ToolName.SOLVE_SCHEDULE: "排程",
    ToolName.SOP_SEARCH: "查 SOP",
    ToolName.QUERY_ORDERS_SHIFTS: "訂單班表",
    ToolName.CONFIRM_SCHEDULE: "確認",
    ToolName.SET_CURE_TIME: "設定硫化時間",
    ToolName.NOTIFY_TEAMS: "通知",
}
CHANNEL_LABELS = {Channel.TEMP: "溫度", Channel.PRESSURE: "壓力", Channel.WATER_FLOW: "水流",
                  Channel.STEAM: "蒸汽壓"}
NOTIFY_LABELS = {NotifyKind.MAINTENANCE: "通知維修", NotifyKind.INFO: "通知", NotifyKind.SLOW_FEED: "放慢生料投入",
                 NotifyKind.PAUSE_FEED: "暫停投料", NotifyKind.STAFF_MOVE: "跨組調人",
                 NotifyKind.OVERTIME: "加班", NotifyKind.MACHINE_OUT: "機台停用",
                 NotifyKind.DISPATCH: "派工"}
LINE_PREFIX = {Line.A: "A 線", Line.B: "B 線", Line.C: "C 線", Line.D: "D 線", Line.BOND: "貼合", Line.CURE: ""}


def press_no(machine_id: str) -> int | None:
    m = MACHINE_BY_ID.get(machine_id)
    if m is None or m.line is not Line.CURE:
        return None
    return int(machine_id.split("-")[-1])


def machine_label(machine_id: str, short: bool = False) -> str:
    """CURE-07 -> '7 號機' (short: '7 號'); A-BIGOPEN -> 'A 線大開煉機'."""
    n = press_no(machine_id)
    if n is not None:
        return f"{n} 號" if short else f"{n} 號機"
    m = MACHINE_BY_ID.get(machine_id)
    if m is None:
        return "全廠" if machine_id == "PLANT" else machine_id     # issue #10: a command that names no machine
    return f"{LINE_PREFIX[m.line]}{m.name}"


def fmt_num(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:.1f}".rstrip("0").rstrip(".") if abs(f - round(f)) > 1e-9 else f"{int(round(f))}"


def is_approval_tool(step: PlanStep) -> bool:
    if step.tool in (ToolName.CONFIRM_SCHEDULE, ToolName.SET_CURE_TIME):
        return True
    if step.tool is ToolName.NOTIFY_TEAMS:
        return step.args.get("kind") not in (NotifyKind.MAINTENANCE.value, NotifyKind.INFO.value)
    return False


_SUB = re.compile(r"^(S\d+)[a-z]$")


def decompose_text(steps: list[PlanStep]) -> str:
    """'S1 硫化公式、S2 訂單班表 → S3 排程（依賴 S1、S2）→ S4 確認（需核准）→ S5 通知'."""
    ids = {s.id for s in steps}
    # a sub-step ("S4b") belongs to its parent's approval (set_cure_time with S4 確認): not listed apart
    shown = [s for s in steps if not (_SUB.match(s.id) and _SUB.match(s.id).group(1) in ids)]
    level: dict[str, int] = {}
    for s in steps:   # steps are in dependency order in practice; fall back to 0
        level[s.id] = 1 + max((level.get(d, 0) for d in s.depends_on), default=-1)
    groups: dict[int, list[str]] = {}
    for s in shown:
        label = f"{s.id} {TOOL_LABELS.get(s.tool, s.tool.value)}"
        if s.tool is ToolName.NOTIFY_TEAMS and s.args.get("kind") not in (None, "info"):
            label = f"{s.id} {NOTIFY_LABELS.get(NotifyKind(s.args['kind']), '通知')}"
        notes = []
        if s.depends_on and s.tool in (ToolName.SOLVE_SCHEDULE,):
            notes.append(f"依賴 {'、'.join(s.depends_on)}")
        if is_approval_tool(s):
            notes.append("需核准")
        if notes:
            label += f"（{'；'.join(notes)}）"
        groups.setdefault(level[s.id], []).append(label)
    return " → ".join("、".join(groups[k]) for k in sorted(groups)).replace("） → ", "）→ ")


def expectation_text(exp: Any) -> str:
    if exp is None:
        return ""
    op = {">=": "≥", "<=": "≤"}.get(exp.op, exp.op)
    unit = "°C" if exp.channel is Channel.TEMP else ""
    val = (f"{fmt_num(exp.value)}–{fmt_num(exp.value_hi)}{unit}" if exp.op == "between"
           else f"{op}{fmt_num(exp.value)}{unit}")
    return f"{fmt_num(exp.within_min)} 分內 {val}"


def result_text(tool: ToolName, args: dict[str, Any], data: dict[str, Any], today: date | None = None) -> str:
    """One short phrase for a successful tool result."""
    if tool is ToolName.CURE_FORMULA:
        return (f"{fmt_num(args.get('actual_temp_c'))}°C 總需 {fmt_num(data.get('total_cure_h'))} h"
                f"（+{fmt_num(data.get('extra_h'))} h）")
    if tool is ToolName.QUERY_ORDERS_SHIFTS:
        parts = []
        jobs = data.get("next_jobs") or []
        orders = {o.get("id"): o for o in data.get("orders") or []}
        if jobs:
            oid = jobs[0].get("order_id")
            due = (orders.get(oid) or {}).get("due")
            txt = f"下一模 {oid}"
            if due and today:
                try:
                    txt += f" 交期 {(date.fromisoformat(str(due)) - today).days} 天後"
                except ValueError:
                    pass
            parts.append(txt)
        parts += [str(g) for g in data.get("gaps") or []]
        return "，".join(parts) or "訂單班表無異動"
    if tool is ToolName.READ_SENSORS:
        summ = data.get("summary") or {}
        bits = []
        for mid, s in summ.items():
            now = s.get("temp_c_now")
            bits.append(f"{machine_label(mid, short=True)} {fmt_num(now)}°C" if now is not None
                        else machine_label(mid, short=True))
        return "、".join(bits) or "感測器讀值"
    if tool is ToolName.SOP_SEARCH:
        hits = data.get("hits") or []
        return "、".join(f"SOP「{h.get('title')}」" for h in hits[:1]) or "SOP 無相符"
    if tool is ToolName.SOLVE_SCHEDULE:
        return solver_text(data)
    if tool is ToolName.PREDICT_ANOMALY:
        return f"預測 {fmt_num(data.get('predicted_value'))}（{fmt_num(data.get('band_lo'))}–{fmt_num(data.get('band_hi'))}）"
    if tool is ToolName.NOTIFY_TEAMS:
        teams = "、".join(TEAM_LABELS.get(t, str(t)) for t in _teams(args.get("teams")))
        return f"{NOTIFY_LABELS.get(NotifyKind(args.get('kind', 'info')), '通知')} → {teams}：{args.get('message', '')}"
    if tool is ToolName.CONFIRM_SCHEDULE:
        return f"排程 v{args.get('schedule_version')} 生效（上一版保留，可一鍵回復）"
    if tool is ToolName.SET_CURE_TIME:
        return f"{machine_label(str(args.get('press_id')))} 建議硫化時間 {fmt_num(args.get('total_cure_h'))} h"
    return tool.value


def _teams(v: Any) -> list[Any]:
    from warroom.contracts import TeamId
    out = []
    for t in v or []:
        try:
            out.append(TeamId(t))
        except ValueError:
            out.append(t)
    return out


def solver_text(data: dict[str, Any]) -> str:
    bits = []
    retimed = 0
    grouped: dict[tuple[str, str], int] = {}
    for mv in data.get("moves") or []:
        to = mv.get("to_press")
        if to and mv.get("from_press") == to:     # same press, new time: summarised below
            retimed += 1
            continue
        key = (str(mv.get("order_id")), str(to) if to else "")
        grouped[key] = grouped.get(key, 0) + 1
    for (oid, to), n in grouped.items():
        bits.append(f"{oid}{f' ×{n}' if n > 1 else ''} 移到 {machine_label(to) if to else '?'}")
    if retimed:
        bits.append(f"另 {retimed} 模同機微調時間")
    sched = data.get("schedule") or {}
    for c in sched.get("staffing_changes") or []:
        bits.append(staffing_label(c))
    feasible = bool(data.get("feasible", False))
    if not feasible:
        # an infeasible plan meets no delivery: never print 交期全部滿足 next to 不可行. The status
        # can still read 'feasible' when the solver found a plan the independent check rejected.
        warns = (data.get("schedule") or {}).get("warnings") or []
        why = next((str(w).removeprefix("檢核失敗：") for w in warns if str(w).startswith("檢核失敗：")), None)
        if why is None and status_is_error(data):
            why = next((g for g in data.get("gaps") or [] if g), None)
        status = str(data.get("status") or "")
        bits.append(f"模擬不可行（{why or (status if status and status != 'feasible' else '檢查未通過')}）")
    elif data.get("deliveries_met"):
        bits.append("交期全部滿足")
    elif data.get("late_orders"):
        bits.append(f"延誤：{'、'.join(data['late_orders'])}")
    if data.get("fell_back"):
        bits.append("求解逾時 → 已切換備援求解器")
    return "、".join(bits) or "排程無需變動"


def status_is_error(data: dict[str, Any]) -> bool:
    """True when the solver itself failed (no plan); its gaps then carry the error, not staffing."""
    return str(data.get("status") or "") not in ("feasible", "optimal")


def moves_text(moves: list[dict[str, Any]] | None) -> list[str]:
    """Press moves grouped by order and target press, one phrase per order (page 11: '#A-311 改 9 號';
    many molds of one order: '#M-201 ×2 → 8 號、×3 → 9 號'). Same-press time shifts are left out."""
    per_order: dict[str, dict[str, int]] = {}
    new_order: dict[str, dict[str, int]] = {}
    for mv in moves or []:
        to = mv.get("to_press")
        if to and not mv.get("from_press"):          # issue #17: a tyre of an inserted order (no previous slot)
            tgt = new_order.setdefault(str(mv.get("order_id")), {})
            tgt[str(to)] = tgt.get(str(to), 0) + 1
            continue
        if not to or mv.get("from_press") == to:
            continue
        tgt = per_order.setdefault(str(mv.get("order_id")), {})
        tgt[str(to)] = tgt.get(str(to), 0) + 1
    out: list[str] = []

    def where(p: str) -> str:
        n = press_no(p)
        return f"{n} 號" if n else machine_label(p)
    for oid, targets in new_order.items():
        out.append(f"{oid} 排入 " + "、".join(f"{where(p)}" + (f" ×{n}" if n > 1 else "") for p, n in targets.items()))
    for oid, targets in per_order.items():
        if len(targets) == 1 and next(iter(targets.values())) == 1:
            out.append(f"{oid} 改 {where(next(iter(targets)))}")
        else:
            out.append(f"{oid} " + "、".join(f"×{n} → {where(p)}" for p, n in targets.items()))
    return out


def order_shifts(moves: list[dict[str, Any]] | None, jobs: list[dict[str, Any]] | None = None) -> list[str]:
    """Issue #17 (插單 card): per order, how far its last planned tyre moved in time with this solve (+ = later),
    from the moves and the solved jobs: old last = the moves' previous slots plus the unmoved jobs, new last =
    the solved jobs. Inserted orders: '#R-009 新排 2 模（首模 9 號 10/07 14:00）'."""
    moved: dict[str, list[dict[str, Any]]] = {}
    for mv in moves or []:
        moved.setdefault(str(mv.get("order_id")), []).append(mv)
    by_order: dict[str, list[dict[str, Any]]] = {}
    for j in jobs or []:
        if not j.get("frozen"):
            by_order.setdefault(str(j.get("order_id")), []).append(j)

    def ts(v: Any) -> datetime | None:
        try:
            return datetime.fromisoformat(str(v)) if v else None
        except ValueError:
            return None
    out_new: list[str] = []
    shifts: list[tuple[str, float]] = []
    for oid, mvs in moved.items():
        if all(not m.get("from_press") for m in mvs):
            first = min(mvs, key=lambda m: str(m.get("to_mold_in")))
            at = ts(first.get("to_mold_in"))
            p = press_no(str(first.get("to_press") or ""))
            out_new.append(f"{oid} 新排 {len(mvs)} 模（首模 {p} 號 {at:%m/%d %H:%M}）" if at and p else f"{oid} 新排 {len(mvs)} 模")
            continue
        ids = {m.get("job_id") for m in mvs}
        new_times = [t for j in by_order.get(oid, []) if (t := ts(j.get("mold_in")))]
        old_times = [t for m in mvs if (t := ts(m.get("from_mold_in")))]
        old_times += [t for j in by_order.get(oid, []) if j.get("id") not in ids and (t := ts(j.get("mold_in")))]
        if not new_times or not old_times:
            continue
        h = (max(new_times) - max(old_times)).total_seconds() / 3600.0
        if abs(h) >= 0.1:
            shifts.append((oid, h))
    shifts.sort(key=lambda x: -abs(x[1]))
    return out_new + [f"{oid} 完工{'延後' if h > 0 else '提前'} {fmt_num(abs(round(h, 1)))} h" for oid, h in shifts]


def staffing_label(c: dict[str, Any]) -> str:
    label = c.get("label") or ""
    name = c.get("shift_name") or ""
    if name and name not in label:
        return f"{name}{label}"
    return label


_STALE_RE = re.compile(r"schedule v(\d+) is stale: solved against v(\d+), but v(\d+) is active")
_HELD_RE = re.compile(r"schedule v(\d+) is stale: it loads presses that are held now")
_NOT_PROPOSED_RE = re.compile(r"schedule v(\d+) is (\w+), not proposed")


def tool_label(tool: ToolName) -> str:
    return TOOL_LABELS.get(tool, tool.value)


def tool_error_text(tool: ToolName, error: str | None) -> str:
    """Judge-facing Chinese for a failed tool call. The raw exception text stays in the audit log only
    (TOOL_RESULT entries); the timeline never shows a Python error or a truncated English message."""
    e = error or ""
    if m := _STALE_RE.search(e):
        return f"排程 v{m.group(1)} 已過期：求解時依據 v{m.group(2)}，期間 v{m.group(3)} 已生效，需依現行排程重新求解"
    if m := _HELD_RE.search(e):
        return f"排程 v{m.group(1)} 已過期：仍排有停機／停用的硫化機，需重新求解"
    if m := _NOT_PROPOSED_RE.search(e):
        return f"排程 v{m.group(1)} 已不是待確認版本（{m.group(2)}）"
    if "not open in" in e:
        return "此狀態不開放該工具（已攔截並記錄）"
    if "timeout" in e.lower() or "timed out" in e.lower():
        return "逾時"
    if "unavailable in this tool context" in e:
        return "工具暫時無法使用"
    if "unknown machine id" in e:
        return "機台代號不存在"
    if "is not a curing press" in e:
        return "不是硫化機"
    if "out of range" in e:
        return "數值超出允許範圍"
    if "ValidationError" in e or "validation error" in e:
        return "參數格式不符"
    return "執行錯誤（詳見稽核日誌）"


def gate_reason_text(reasons: list[str]) -> str:
    """Chinese for the gate's refusal reasons on the timeline (the English reasons stay in the audit)."""
    out: list[str] = []
    for r in reasons:
        if "allow-list" in r:
            out.append("不在允許清單（已攔截）")
        elif "is not open in state" in r:
            out.append("此狀態不開放該工具")
        elif "unresolved $S reference" in r:
            out.append("引用的前一步結果不存在")
        elif r.startswith("args invalid"):
            out.append("參數格式不符")
        elif r.startswith("unknown machine id"):
            out.append("機台代號不存在")
        elif r.startswith("simulation not feasible"):
            out.append("沒有對應的可行模擬")
        elif "supervisor approval required" in r:
            continue
        else:
            out.append("未通過規則檢查（詳見稽核日誌）")
    return "；".join(dict.fromkeys(out)) or "未通過規則檢查"


def material_dispatch_text(name: str | None) -> str:
    """Issue #32: the 派工 message for a material shortfall the patrol found (fixed text, the same in rule mode
    and when code adds the step the model left out)."""
    n = name or "原料"
    return f"{n}缺料風險：到碼頭盤點{n}、向供應商確認到貨時間，先排不用{n}的批次"
