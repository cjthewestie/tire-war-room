"""End-to-end page-11 scenario in mock mode: press 7 128→122°C, H1 steam pressure low, S1..S5,
supervisor approves at 02:16:40, verify reads 119.5°C at 02:31:00 → H1 rejected, H2 leak / sensor
drift (compare 6 & 8 on the same pipe, SOP 「硫化中斷處置」), press 7 out of service, supervisor
rejects the cross-team move and picks night overtime at 02:31:30. Sim/tools/schedule are fakes."""
from __future__ import annotations

import asyncio

from tests.agent_fakes import T0, FakeRegistry, FakeSim, FakeStore, at
from warroom.agent import ApprovalBrokerImpl, JsonlAuditLog, MockCommander, RuleGate, StateMachine
from warroom.bus import InProcessBus
from warroom.clock import SimClock
from warroom.contracts import (
    Anomaly,
    AnomalyKind,
    ApprovalDecision,
    AuditKind,
    Channel,
    DecisionKind,
    EventState,
    HypothesisStatus,
    MachineStatus,
    NotifyKind,
    Prediction,
    SafetyAction,
    SafetyActionKind,
    Severity,
    ToolName,
    WsType,
)


def press7_temp(mid, now):
    if mid != "CURE-07":
        return 128.0
    if now < T0:
        return 128.0
    if now < at("02:26:00"):
        return 122.0
    if now < at("02:30:30"):
        return 120.5          # below the 121 expectation, sustained 5 min -> reject at 02:31:00
    return 119.5


def page11_anomaly() -> Anomaly:
    pred = Prediction(machine_id="CURE-07", channel=Channel.TEMP, now_value=122.0, predicted_value=122.0,
                      band_lo=121.0, band_hi=123.0, horizon_s=1800, trend_per_min=0.0)
    return Anomaly(id="AN-1", ts=T0, machine_id="CURE-07", kind=AnomalyKind.TEMP_DROP, channel=Channel.TEMP,
                   value=122.0, baseline=128.0, prediction=pred, severity=Severity.WARN,
                   message="7 號 128 → 122°C，預測穩定在 122±1°C")


def build(tmp_path):
    clock = SimClock(start=T0, manual=True)
    bus = InProcessBus(clock=clock)
    audit = JsonlAuditLog(tmp_path / "audit.jsonl", bus=bus, clock=clock)
    sim = FakeSim(clock, press7_temp)
    store = FakeStore(clock)
    tools = FakeRegistry(sim, store)
    approvals = ApprovalBrokerImpl(bus=bus, audit=audit, clock=clock)
    holder = {}

    async def sleep(s):
        clock.advance(s)
        if clock.now() >= at("02:30:30") and not holder.get("fired"):
            holder["fired"] = True
            holder["sm"].on_safety_action(SafetyAction(
                id="SA-1", ts=clock.now(), machine_id="CURE-07", rule_id="cure_low", action=SafetyActionKind.CURE_HOLD,
                value=119.5, threshold=120.0, message="7 號機 119.5°C < 120°C：安全核心扣模", latency_ms=3.0))
        await asyncio.sleep(0)

    sm = StateMachine(MockCommander(), tools, RuleGate(store=store), approvals, audit, sim=sim, bus=bus, clock=clock,
                      store=store, sleep=sleep, approval_timeout_s=30,
                      verify_confirm_s=300)   # the scripted plant crosses 121 °C at exactly 02:26:00
    holder["sm"] = sm
    return clock, bus, audit, sim, store, tools, approvals, sm


async def supervisor(clock, approvals, script):
    """script: list of (time 'HH:MM:SS', decision kind, option_id, reason)."""
    for hms, kind, option, reason in script:
        while not approvals.pending():
            await asyncio.sleep(0.001)
        req = approvals.pending()[0]
        target = at(hms)
        if clock.now() < target:
            clock.advance((target - clock.now()).total_seconds())
        approvals.decide(ApprovalDecision(request_id=req.id, decision=kind, option_id=option, reason=reason))


def _find(timeline, actor, *needles, start=0):
    for i in range(start, len(timeline)):
        it = timeline[i]
        if it.actor == actor and all(n in it.text for n in needles):
            return i
    raise AssertionError(f"no {actor} row with {needles} after {start}:\n" +
                         "\n".join(f"{t.ts:%H:%M:%S} {t.actor} {t.text}" for t in timeline))


async def test_page11_timeline(tmp_path):
    clock, bus, audit, sim, store, tools, approvals, sm = build(tmp_path)
    sup = asyncio.create_task(supervisor(clock, approvals, [
        ("02:16:40", DecisionKind.APPROVE, None, ""),
        ("02:31:30", DecisionKind.APPROVE, "A", "跨組調人影響 2 線，改夜班加班"),
    ]))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await sup

    tl = ev.timeline
    print("\n" + "\n".join(f"{t.ts:%H:%M:%S}  {t.actor:<8} [{t.tag}] {t.text}" for t in tl))

    assert ev.state is EventState.CLOSED
    # page-11 rows, in order
    i = _find(tl, "事件偵測", "128 → 122°C", "預測穩定在 122±1°C", "喚醒指揮官")
    assert tl[i].ts == at("02:14:00")
    i = _find(tl, "安全核心", "仍在 120–130°C", "不停機", start=i)
    assert tl[i].ts == at("02:14:01")
    i = _find(tl, "指揮官", "讀 30 分鐘溫度", "查 SOP", "假設 H1：蒸汽壓不足，溫度會穩定", start=i)
    assert tl[i].ts == at("02:14:03")
    i = _find(tl, "指揮官", "拆步驟：S1 硫化公式、S2 訂單班表 → S3 排程（依賴 S1、S2）→ S4 確認（需核准）、S5 設定硫化時間（需核准）→ S6 通知",
              "預期：30 分內 ≥121°C", start=i)
    assert tl[i].ts == at("02:14:09")
    i = _find(tl, "工具", "S1：122°C 總需 36.5 h（+8.5 h）", "S2：下一模 #A-311 交期 3 天後", "夜班缺 1 人", start=i)
    assert tl[i].ts == at("02:14:11")
    i = _find(tl, "求解器", "S3：#A-311 移到 9 號機", "夜班從 2 線調 1 人", "交期全部滿足", start=i)
    assert tl[i].ts == at("02:14:25")
    g = _find(tl, "系統", "動作把關", start=i)
    assert tl[g].tag == "human"            # gate row is 權限＋人工, not 任務分解
    # timestamps never run backwards between the commander's beats
    assert all(a.ts <= b.ts for a, b in zip(tl[:g], tl[1:g + 1]))
    i = _find(tl, "主管", "動作把關確認可行", "主管核准", "寫入稽核日誌", start=i)
    assert tl[i].ts == at("02:16:40")
    i = _find(tl, "驗證（程式）", "119.5°C", "H1 不成立", "安全核心已依規則處置", start=i)
    assert tl[i].ts == at("02:31:00")
    i = _find(tl, "指揮官", "推翻 H1 → H2：持續洩壓或感測器漂移", "對照 6、8 號同管線", "SOP「硫化中斷處置」",
              "7 號機停用", start=i)
    i = _find(tl, "主管", "否決跨組調人，改選夜班加班", "決定與理由寫入紀錄", start=i)
    assert tl[i].ts == at("02:31:30")

    # hypotheses
    h1, h2 = ev.hypotheses
    assert (h1.id, h1.cause_code, h1.status) == ("H1", "steam_pressure_low", HypothesisStatus.REJECTED)
    assert h1.expectation.op == ">=" and h1.expectation.value == 121 and h1.expectation.within_min == 30
    assert any("119.5" in e for e in h1.evidence)
    assert (h2.id, h2.cause_code) == ("H2", "steam_leak_or_sensor_drift")
    assert ev.replans == 1 and ev.consecutive_failures == 0     # round 2 executed cleanly: streak over

    # S1 numbers came from the formula tool, S3 schedule override referenced $S1.total_cure_h
    solve1 = tools.calls_of(ToolName.SOLVE_SCHEDULE)[0]
    assert solve1.args["cure_overrides_h"] == {"CURE-07": 36.5}
    solve2 = tools.calls_of(ToolName.SOLVE_SCHEDULE)[1]
    assert solve2.args["unavailable_presses"] == ["CURE-07"]

    # approvals: round 1 approved the solver's pick, round 2 card matches the phone on page 11
    reqs = approvals.all()
    assert len(reqs) == 2
    r2 = reqs[1]
    assert r2.round == 2 and r2.title == "7 號機停用，重排中"
    assert r2.reason == "H1 不成立：溫度續降至 119.5°C"
    assert "安全核心已處置" in r2.done and "#A-311 改 9 號" in r2.done
    assert "10/03 夜班缺 1 人" in r2.gaps and r2.delivery_impact == "不受影響"
    assert [(o.id, o.label) for o in r2.options] == [("A", "夜班加班"), ("B", "從 2 線調 1 人")]
    assert r2.decision.option_id == "A" and r2.decision.reason
    assert reqs[0].decision.option_id == "B"         # round 1: solver's transfer, approved

    # executed writes: round 2 sent overtime, not a staff move; press 7 out of service
    notifies = [c.args["kind"] for c in tools.calls_of(ToolName.NOTIFY_TEAMS)]
    # round 1: chosen transfer (B) then S5 info (waits for S4's approval); round 2: machine out + overtime (A)
    # round 2 then withdraws the round-1 transfer with an info notice, so only overtime stands
    assert notifies == [NotifyKind.STAFF_MOVE.value, NotifyKind.INFO.value,
                        NotifyKind.MACHINE_OUT.value, NotifyKind.OVERTIME.value, NotifyKind.INFO.value]
    revoke = tools.calls_of(ToolName.NOTIFY_TEAMS)[-1]
    assert revoke.args["message"].startswith("撤回：") and "夜班加班" in revoke.args["message"]
    assert revoke.args["teams"] == tools.calls_of(ToolName.NOTIFY_TEAMS)[0].args["teams"]   # same teams as the move
    _find(tl, "系統", "撤回先前已執行的「從 2 線調 1 人」")
    _find(tl, "系統", "排程 v3 人力依主管決定改為「夜班加班」", "取代「從 2 線調 1 人」")
    # the outcome and the live schedule carry the supervisor's decision, not the solver's transfer
    assert "從 2 線調 1 人" not in ev.outcome.replace("撤回從 2 線調 1 人", "")
    assert "夜班加班" in ev.outcome and "撤回從 2 線調 1 人" in ev.outcome
    live = store.get(3).schedule.staffing_changes
    assert [(c.kind, c.label) for c in live] == [("overtime", "夜班加班")]
    assert "夜班加班" in store.get(3).reason
    assert [(c.kind, c.label) for c in store.get(2).schedule.staffing_changes] == [("transfer", "從 2 線調 1 人")]
    assert sim.status["CURE-07"] is MachineStatus.OUT_OF_SERVICE
    assert store.active == 3                                # second solve's version is live

    # writes only ever ran in EXECUTE, and the audit chain is intact
    entries = audit.tail(0, 0)
    for e in entries:
        if e.kind is AuditKind.TOOL_CALL and e.payload["args"] and e.actor.split(":")[1] in (
                "confirm_schedule", "set_cure_time", "notify_teams"):
            assert e.payload["state"] == "execute"
    kinds = [e.kind for e in entries]
    assert kinds.count(AuditKind.LLM_CALL) == 2 and kinds.count(AuditKind.APPROVAL_DECISION) == 2
    assert AuditKind.SAFETY_ACTION in kinds
    assert audit.verify_chain()
    transitions = [e.payload["to"] for e in entries if e.kind is AuditKind.STATE_TRANSITION]
    assert transitions == ["detected", "classify", "plan", "simulate", "gate", "await_approval", "execute", "verify",
                           "replan", "simulate", "gate", "await_approval", "execute", "verify", "closed"]
    # the dashboard / phone saw it live
    types = {m.type for m in bus.history}
    assert {WsType.EVENT, WsType.ALERT_CARD, WsType.APPROVAL_REQUEST, WsType.APPROVAL_RESOLVED, WsType.AUDIT} <= types
    cards = [m.data["card"]["title"] for m in bus.history if m.type is WsType.ALERT_CARD]
    assert "7 號機溫度下降 128→122°C" in cards and cards[-1] == "7 號機停用，重排中"


async def test_page11_two_misses_safe_stop_keeps_schedule(tmp_path):
    """Supervisor rejects round 1 and round 2 → 2 consecutive failures → SAFE_STOP, original schedule."""
    clock, bus, audit, sim, store, tools, approvals, sm = build(tmp_path)
    sup = asyncio.create_task(supervisor(clock, approvals, [
        ("02:16:40", DecisionKind.REJECT, None, "不同意"),
        ("02:18:00", DecisionKind.REJECT, None, "仍不同意"),
    ]))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await sup
    assert ev.state is EventState.SAFE_STOP
    assert "維持原排程" in ev.outcome
    assert store.active == 1                                 # nothing confirmed, original kept
    assert not tools.calls_of(ToolName.CONFIRM_SCHEDULE)
    assert audit.verify_chain()


async def test_duplicate_anomaly_attaches_to_open_event(tmp_path):
    clock, bus, audit, sim, store, tools, approvals, sm = build(tmp_path)
    task = asyncio.create_task(sm.handle(page11_anomaly()))
    while not approvals.pending():                 # event is open, waiting for round-1 approval
        await asyncio.sleep(0.001)
    again = await sm.handle(page11_anomaly().model_copy(update={"id": "AN-2"}))
    sup = asyncio.create_task(supervisor(clock, approvals, [
        ("02:16:40", DecisionKind.APPROVE, None, ""), ("02:31:30", DecisionKind.APPROVE, "A", "")]))
    ev = await asyncio.wait_for(task, 10)
    await sup
    assert again.id == ev.id and len(sm.events()) == 1
    assert any("追加異常" in t.text for t in ev.timeline)


async def test_page11_with_real_sim_detector_and_safety(tmp_path):
    """Integration: real Factory (press7_temp_drop), StreamDetector and RuleSafetyCore drive the
    state machine; only the tool registry is a fake (reads come from the real sim)."""
    from warroom.contracts import ScenarioName
    from warroom.detect import StreamDetector
    from warroom.safety import RuleSafetyCore
    from warroom.sim import Factory, get_scenario

    sc = get_scenario(ScenarioName.PRESS7_TEMP_DROP)
    clock = SimClock(start=sc.start_at, manual=True)
    plant = Factory(clock=clock, scenario=ScenarioName.PRESS7_TEMP_DROP)
    det, safety = StreamDetector(clock=clock), RuleSafetyCore(clock=clock)
    bus = InProcessBus(clock=clock)
    audit = JsonlAuditLog(tmp_path / "audit.jsonl", bus=bus, clock=clock)
    store = FakeStore(clock)
    tools = FakeRegistry(plant, store)
    approvals = ApprovalBrokerImpl(bus=bus, audit=audit, clock=clock)
    holder = {}

    def tick():
        rs = plant.step(1.0)
        for act in safety.check(rs, {m.machine_id: m for m in plant.machines()}):
            plant.apply_safety_action(act)
            if "sm" in holder:
                holder["sm"].on_safety_action(act)
        return det.ingest(rs)

    async def sleep(s):
        for _ in range(int(s)):
            tick()
        await asyncio.sleep(0)

    sm = StateMachine(MockCommander(), tools, RuleGate(store=store), approvals, audit, sim=plant, bus=bus,
                      clock=clock, store=store, sleep=sleep)
    holder["sm"] = sm

    anomaly = None
    while anomaly is None and clock.now() < at("02:20:00"):
        anomaly = next((a for a in tick() if a.machine_id == "CURE-07"), None)
    assert anomaly is not None and anomaly.kind is AnomalyKind.TEMP_DROP

    async def sup():
        for hms, option in (("02:16:40", None), ("02:31:30", "A")):
            while not approvals.pending():
                await asyncio.sleep(0.001)
            while clock.now() < at(hms):
                tick()
            approvals.decide(ApprovalDecision(request_id=approvals.pending()[0].id, decision=DecisionKind.APPROVE,
                                              option_id=option, reason="夜班加班" if option else ""))
    t = asyncio.create_task(sup())
    ev = await asyncio.wait_for(sm.handle(anomaly), 20)
    await t
    print("\n" + "\n".join(f"{x.ts:%H:%M:%S}  {x.actor:<8} {x.text}" for x in ev.timeline))

    assert ev.state is EventState.CLOSED
    v = _find(ev.timeline, "驗證（程式）", "H1 不成立")
    assert at("02:30:00") <= ev.timeline[v].ts <= at("02:32:00")
    assert ev.hypotheses[0].status is HypothesisStatus.REJECTED and ev.hypotheses[1].cause_code.startswith("steam_leak")
    assert ev.safety_actions, "safety core should have acted below 120°C before verify"
    assert tools.calls_of(ToolName.CURE_FORMULA)[0].args["actual_temp_c"] == 122.0
    assert plant.machine("CURE-07").status is MachineStatus.OUT_OF_SERVICE
    assert audit.verify_chain()
