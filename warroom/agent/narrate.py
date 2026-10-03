"""Chinese labels and timeline sentences for the page-11 style event timeline.

Pure formatting: every number shown comes from a tool result (formula / solver / sensors), never
from the model. Formatters are defensive (``.get``) because tool payloads are dicts."""
from __future__ import annotations

from datetime import date
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
                 NotifyKind.OVERTIME: "加班", NotifyKind.MACHINE_OUT: "機台停用"}
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
        return machine_id
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


def decompose_text(steps: list[PlanStep]) -> str:
    """'S1 硫化公式、S2 訂單班表 → S3 排程（依賴 S1、S2）→ S4 確認（需核准）→ S5 通知'."""
    level: dict[str, int] = {}
    for s in steps:   # steps are in dependency order in practice; fall back to 0
        level[s.id] = 1 + max((level.get(d, 0) for d in s.depends_on), default=-1)
    groups: dict[int, list[str]] = {}
    for s in steps:
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
    for mv in data.get("moves") or []:
        to = mv.get("to_press")
        bits.append(f"{mv.get('order_id')} 移到 {machine_label(to) if to else '?'}")
    sched = data.get("schedule") or {}
    for c in sched.get("staffing_changes") or []:
        bits.append(staffing_label(c))
    if data.get("deliveries_met"):
        bits.append("交期全部滿足")
    elif data.get("late_orders"):
        bits.append(f"延誤：{'、'.join(data['late_orders'])}")
    if not data.get("feasible", False):
        bits.append(f"不可行（{data.get('status', '?')}）")
    if data.get("fell_back"):
        bits.append("求解逾時 → 已切換備援求解器")
    return "、".join(bits) or "排程無需變動"


def staffing_label(c: dict[str, Any]) -> str:
    label = c.get("label") or ""
    name = c.get("shift_name") or ""
    if name and name not in label:
        return f"{name}{label}"
    return label
