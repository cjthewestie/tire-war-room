"""Demo-run fixes (10-21 walk-through, group B): a real model's solve without the cure override still
gets the formula value; a press H1 notifies maintenance so the steam fault gets a repair task; a card that
must be re-approved because another event's schedule took effect first is a new version of the same card."""
from __future__ import annotations

import asyncio

from tests.test_agent import auto_approve, make_sm
from tests.test_agent_page11 import page11_anomaly
from warroom.agent import MockCommander
from warroom.contracts import AuditKind, EventState, NotifyKind, ToolName

stable = lambda m, now: 122.0 if m == "CURE-07" else 128.0


class NoOverride(MockCommander):
    """What the real model sent in 2 of 3 page-11 runs: S3 solve_schedule without cure_overrides_h."""

    def build(self, req):
        p = super().build(req)
        if req.phase != "plan":
            return p
        steps = [s.model_copy(update={"args": {k: v for k, v in s.args.items() if k != "cure_overrides_h"}})
                 if s.tool is ToolName.SOLVE_SCHEDULE else s for s in p.steps]
        return p.model_copy(update={"steps": steps})


async def test_solve_without_cure_override_gets_the_formula_value(tmp_path):
    _clock, audit, _sim, _store, tools, approvals, sm = make_sm(tmp_path, stable, commander=NoOverride())
    sup = asyncio.create_task(auto_approve(approvals, 1))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await sup
    assert ev.state is EventState.CLOSED
    assert tools.calls_of(ToolName.SOLVE_SCHEDULE)[0].args["cure_overrides_h"] == {"CURE-07": 36.5}
    filled = [e.payload for e in audit.tail(0, 0) if "cure_overrides_from_formula" in e.payload]
    assert filled and filled[0]["cure_overrides_from_formula"] == {"CURE-07": {"from": None, "to": 36.5}}
    card = approvals.all()[0]
    # the card keeps the cure time and the schedule, not just 「通知維修」 with 「排程未變更」
    assert any(a.startswith("S4b") and "36.5 h" in a for a in card.actions), card.actions
    assert any("7 號硫化 36.5 h" in a for a in card.actions), card.actions
    assert card.delivery_impact == "不受影響"
    assert [c.args["total_cure_h"] for c in tools.calls_of(ToolName.SET_CURE_TIME)] == [36.5]


async def test_solve_that_takes_the_press_out_gets_no_cure_override(tmp_path):
    """Round 2 (H2, press out of service) has no formula result and takes the press out: nothing is filled."""
    from tests.test_agent_page11 import press7_temp
    _clock, audit, _sim, _store, tools, approvals, sm = make_sm(tmp_path, press7_temp, commander=NoOverride())
    sup = asyncio.create_task(auto_approve(approvals, 2))
    await asyncio.wait_for(sm.handle(page11_anomaly()), 20)
    await sup
    solves = tools.calls_of(ToolName.SOLVE_SCHEDULE)
    out = [c.args for c in solves if "CURE-07" in (c.args.get("unavailable_presses") or [])]
    assert out and all("CURE-07" not in (a.get("cure_overrides_h") or {}) for a in out)
    assert not any(e.kind is AuditKind.GATE and e.payload.get("step_id") == "S4b"
                   and e.payload.get("dropped") == "rejected" for e in audit.tail(0, 0))
    assert NotifyKind.MACHINE_OUT.value in [c.args["kind"] for c in tools.calls_of(ToolName.NOTIFY_TEAMS)]


# ---------------------------------------------------------------- a press H1 notifies maintenance
class _Board:
    """Stands in for World.tasks: records what the state machine dispatches and withdraws."""

    def __init__(self):
        self.dispatched: list[tuple[str | None, str | None]] = []
        self.cancelled: list[str] = []

    def dispatch(self, req, now):
        self.dispatched.append((req.step_id, req.args.get("kind")))
        return []

    def cancel(self, event_id, step_id, now):
        hits = [s for s, k in self.dispatched if s == step_id and k == NotifyKind.MAINTENANCE.value]
        self.cancelled += hits
        return [f"TK-{step_id}"] if hits else []


def _with_board(sim):
    from types import SimpleNamespace
    board = _Board()
    sim.world = SimpleNamespace(tasks=board)
    return board


async def test_press_h1_notifies_maintenance_once_without_an_option(tmp_path):
    """The rule plan (and a real model's) has no maintenance step for a press: code adds the R1 notice, it runs
    before the card (no extra option), and a supervisor reject that re-plans H1 does not send it again."""
    from warroom.contracts import ApprovalDecision, DecisionKind
    _clock, _audit, sim, _store, tools, approvals, sm = make_sm(tmp_path, stable)
    board = _with_board(sim)

    async def sup():
        while not approvals.pending():
            await asyncio.sleep(0.001)
        approvals.decide(ApprovalDecision(request_id=approvals.pending()[0].id, decision=DecisionKind.REJECT,
                                          reason="不跨組"))
        await auto_approve(approvals, 1)
    t = asyncio.create_task(sup())
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await t
    maint = [c for c in tools.calls_of(ToolName.NOTIFY_TEAMS) if c.args["kind"] == NotifyKind.MAINTENANCE.value]
    assert len(maint) == 1, [c.args for c in maint]
    assert maint[0].args == {"teams": ["maintenance"], "kind": "maintenance", "machine_id": "CURE-07",
                             "message": "7 號機溫度下降，請檢查蒸汽供應與溫度計"}
    assert maint[0].step_id == "S6"
    first = approvals.all()[0]
    assert [o.label for o in first.options] == ["夜班加班", "從 2 線調 1 人"]        # no option added
    assert not any("通知維修" in a for a in first.actions)
    assert "已通知維修" in first.done
    assert (maint[0].step_id, NotifyKind.MAINTENANCE.value) in board.dispatched     # a 檢修 task for the crew
    assert ev.state is EventState.CLOSED and "已通知維修" in ev.outcome
    assert not board.cancelled


async def test_press_out_withdraws_the_open_h1_repair_task(tmp_path):
    """Page 11: H1's 檢修 must not finish (and clear the fault) while the 7 號機停用 card waits; the press-out
    notice creates 停用檢修 instead."""
    from tests.test_agent_page11 import press7_temp
    _clock, _audit, sim, _store, tools, approvals, sm = make_sm(tmp_path, press7_temp)
    board = _with_board(sim)
    sup = asyncio.create_task(auto_approve(approvals, 2))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 20)
    await sup
    assert board.cancelled == ["S6"]
    assert any("改為停用檢修" in t.text for t in ev.timeline)
    kinds = [c.args["kind"] for c in tools.calls_of(ToolName.NOTIFY_TEAMS)]
    assert kinds.count(NotifyKind.MAINTENANCE.value) == 1 and NotifyKind.MACHINE_OUT.value in kinds


async def test_mill_event_gets_no_press_maintenance_step(tmp_path):
    from tests.test_agent import mill_anomaly
    _clock, audit, _sim, _store, _tools, approvals, sm = make_sm(tmp_path, lambda m, now: 115.0)
    sup = asyncio.create_task(auto_approve(approvals, 3))
    await asyncio.wait_for(sm.handle(mill_anomaly()), 10)
    sup.cancel()
    assert not any(e.payload.get("maintenance_notice_added") for e in audit.tail(0, 0))


# ---------------------------------------------------------------- re-approval after another event's schedule
def _stale_rig(tmp_path, staffing: bool = True):
    """The approved schedule goes stale (another event's version took effect first) and the re-solve moves
    #A-311 elsewhere: the content differs, so the supervisor must approve again."""
    from warroom.contracts import ToolResult
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable)
    real_confirm, real_solve = tools._confirm_schedule, tools._solve_schedule
    state = {"confirm": 0, "solve": 0}

    def flaky(a):
        state["confirm"] += 1
        if state["confirm"] <= 2:
            raise RuntimeError("schedule v2 is stale: solved against v1, but v9 is active now")
        return real_confirm(a)

    def solve(a):
        state["solve"] += 1
        d = real_solve(a)
        if state["solve"] >= 2:
            d["moves"] = [{**d["moves"][0], "to_press": "CURE-10"}]
        if not staffing:
            d["staffing_options"], d["gaps"] = [], []
            d["schedule"]["staffing_changes"] = []
        return d
    tools._confirm_schedule, tools._solve_schedule = flaky, solve
    real_call = tools.call

    def call(c):
        try:
            return real_call(c)
        except RuntimeError as e:
            return ToolResult(call_id=c.id, tool=c.tool, ok=False, error=f"ToolError: {e}", attempt=c.attempt)
    tools.call = call
    return clock, audit, sim, store, tools, approvals, sm


async def test_stale_reapproval_is_v2_of_the_same_card_and_keeps_the_staffing_choice(tmp_path):
    _clock, _audit, _sim, _store, _tools, approvals, sm = _stale_rig(tmp_path)
    sup = asyncio.create_task(auto_approve(approvals, 1, option="A"))     # the supervisor picks 夜班加班
    task = asyncio.create_task(sm.handle(page11_anomaly()))
    await sup
    while len(approvals.all()) < 2:
        await asyncio.sleep(0.001)
    v1, v2 = approvals.all()
    assert (v2.round, v2.version, v2.supersedes) == (v1.round, 2, v1.id)
    assert v2.id == f"{v1.id}v2" and v2.title == v1.title
    assert v2.changes[0].startswith("另一事件的排程先生效；7 號依現行排程重算：#A-311：9 號 → 10 號"), v2.changes
    assert "人力沿用主管上次的選擇：A 夜班加班" in v2.changes
    assert [(o.id, o.label, o.recommended) for o in v2.options] == [("A", "夜班加班", True), ("B", "從 2 線調 1 人", False)]
    await auto_approve(approvals, 1, option="A")
    ev = await asyncio.wait_for(task, 10)
    assert ev.state is EventState.CLOSED and ev.consecutive_failures == 0
    texts = [t.text for t in ev.timeline]
    assert not any(t.startswith("動作未完成") for t in texts), texts     # not shown as a failed approval
    assert any("依現行排程重算（不算失敗）" in t for t in texts)
    assert any(t.startswith("排程已被另一事件更新 → 依現行排程重算（H1") for t in texts), texts
    assert any(t.startswith("新核准卡 v2 取代 v1：") for t in texts)
    summary = [t for t in texts if t.startswith(f"{ev.id}（v2）｜請核准：")]
    assert summary and summary[0].endswith("人力：A 夜班加班。"), summary


async def test_stale_reapproval_with_one_option_says_run_the_new_schedule(tmp_path):
    _clock, _audit, _sim, _store, _tools, approvals, sm = _stale_rig(tmp_path, staffing=False)
    sup = asyncio.create_task(auto_approve(approvals, 2))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await sup
    v1, v2 = approvals.all()
    assert v1.options[0].label.startswith("第 2 版排程生效，"), v1.options
    assert [o.label for o in v2.options] == ["照新排程執行"] and v2.version == 2
    assert not any("人力方案" in c for c in v2.changes), v2.changes       # a lone approve-all is no 人力方案
    assert ev.state is EventState.CLOSED


async def test_card_delivery_line_says_how_late(tmp_path):
    _clock, _audit, _sim, _store, tools, approvals, sm = make_sm(tmp_path, stable)
    real_solve = tools._solve_schedule

    def late_solve(a):
        d = real_solve(a)
        d["deliveries_met"], d["late_orders"] = False, ["#A-311"]
        d["moves"] = [{**d["moves"][0], "late_h": 129.25}]
        return d
    tools._solve_schedule = late_solve
    sup = asyncio.create_task(auto_approve(approvals, 1))
    await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await sup
    assert approvals.all()[0].delivery_impact == "延誤：#A-311（晚 129 小時）"
