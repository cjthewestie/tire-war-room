"""Fast repair (Kyle 10-21 晚): 觸發異常 → 異常發生 → 主管核准 → NemoClaw 派人進廠維修（約 12 秒，3D 圈圈填滿）→ 維修完成 →
回溫中（約 4 秒，琥珀）→ 恢復正常才綠燈 → 異常報告.

Settings.repair_fast_s > 0: a repair task (檢修 / 停用檢修 / 放慢投料 / 暫停投料) starts once the supervisor decided and is done
repair_fast_s WALL seconds later at any sim speed; once every repair task of the event is done the fault is cleared
(repaired_at, 「維修完成，故障排除」) and the machine's readings ramp from where they are to nominal over recover_fast_s wall
seconds (WARNING, 回溫中); back to normal: recovered_at, 「已恢復正常」, VERIFY confirms at once, the event closes
「已修復：…（維修完成 hh:mm、恢復 hh:mm）」 and the machine is RUNNING; then 「異常報告已產生」 with review pending (報告待讀,
nothing waits for it) and POST /api/events/{id}/review marks it read (「主管已讀報告」). repair_fast_s=0 keeps the real
durations. The flows below run the real wall-clock loop, like the demo box."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

from warroom.api import Runtime
from warroom.api.app import create_app
from warroom.config import Settings
from warroom.contracts import (
    ApprovalStatus,
    AuditKind,
    EventState,
    FaultKind,
    FaultSpec,
    GateDecision,
    GateVerdict,
    MachineStatus,
    PlanStep,
    RiskLevel,
    ScenarioName,
    StepRun,
    ToolName,
)
from warroom.factory import CURE_PRESS_IDS
from warroom.sim import Factory
from warroom.sim.events import curing
from warroom.sim.work import REPAIR_KINDS, TaskRequest

FAST_S = 2.0
RECOVER_S = 1.5
SLACK_S = 3.0          # approve -> closed may take this much longer than FAST_S + RECOVER_S (a loaded laptop)
PIN = "fast-repair-pin"
LAN = ("192.168.1.20", 5000)


def _settings(tmp_path, fast_s: float = FAST_S, recover_s: float = RECOVER_S, **kw) -> Settings:
    return Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched", qdrant_path=tmp_path / "qdrant",
                    scenario="normal", llm_mode="mock", repair_fast_s=fast_s, recover_fast_s=recover_s, **kw)


def _client(app, client=("127.0.0.1", 1)) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=client), base_url="http://local")


async def _kb_ready(rt) -> None:
    for _ in range(300):                                              # the demo box has its SOP index ready
        if rt._kb_ok is not None:
            return
        await asyncio.sleep(0.2)


async def _approve(local, a) -> None:
    opt = next((o["id"] for o in a["options"] if o.get("recommended")), a["options"][0]["id"] if a["options"] else None)
    r = await local.post(f"/api/approvals/{a['id']}/decision",
                         json={"request_id": a["id"], "decision": "approve", "option_id": opt})
    assert r.status_code == 200, r.text


async def _fast_flow(tmp_path, scenario: str, speed: float, machine: str, check=None) -> None:
    """Load the scenario like the 觸發異常 button (preroll, demo speed), approve every card as it comes, and time the
    event on `machine` from its approval to CLOSED (the machine 回溫中 in between); then `check(rt, app, ev)`."""
    rt = Runtime(_settings(tmp_path), allow_fallback=False)          # run_loop: the real wall-clock loop
    app = create_app(rt, supervisor_pin=PIN)
    await rt.start()
    try:
        await _kb_ready(rt)
        await rt.load_scenario(ScenarioName(scenario))
        await rt.preroll()
        rt.set_speed(speed)
        approved_at: float | None = None
        repaired_at: float | None = None
        warning = False
        async with _client(app) as local:
            t0 = time.monotonic()
            while time.monotonic() - t0 < 90:
                for a in (await local.get("/api/approvals", params={"status": "pending"})).json():
                    await _approve(local, a)
                    if rt.state_machine.get(a["event_id"]).machine_id == machine and approved_at is None:
                        approved_at = time.monotonic()
                ev = next((e for e in rt.events() if e.machine_id == machine), None)
                if ev is not None and ev.repaired_at is not None and repaired_at is None:
                    repaired_at = time.monotonic()
                tile = next(t for t in (await local.get("/api/machines")).json() if t["machine_id"] == machine)
                if repaired_at is not None and ev.state is not EventState.CLOSED and tile["status"] == "warning":
                    warning = True                                    # 回溫中 (amber) between the fix and green
                if ev is not None and ev.state in (EventState.CLOSED, EventState.SAFE_STOP):
                    break
                await asyncio.sleep(0.05)
            closed_at = time.monotonic()
        assert ev is not None and approved_at is not None, [e.id for e in rt.events()]
        assert ev.state is EventState.CLOSED, ev.outcome
        took = closed_at - approved_at
        assert FAST_S + RECOVER_S - 1.0 <= took <= FAST_S + RECOVER_S + SLACK_S, \
            f"approve -> closed took {took:.1f} s at {speed}x"
        assert repaired_at is not None and closed_at - repaired_at >= RECOVER_S - 0.5    # the ramp, not a snap
        assert warning, "no 回溫中 (warning) between 維修完成 and green"
        assert ev.outcome.startswith("已修復：") and "、恢復 " in ev.outcome, ev.outcome
        assert ev.repaired_at is not None and ev.recovered_at is not None and ev.repaired_at <= ev.recovered_at
        assert ev.review is not None and ev.review.status == "pending" and ev.review.done_at is None and ev.review.by is None
        assert rt.sim.machine(machine).status is MachineStatus.RUNNING                  # green
        drawer = rt.machine_detail(machine).state                                      # the machine drawer's banner
        assert drawer.status is MachineStatus.RUNNING and "回溫中" not in (drawer.note or ""), drawer.note
        tasks = [t for t in rt.sim.world.tasks.for_event(ev.id) if t.kind in REPAIR_KINDS]
        assert tasks and all(t.status == "完成" and t.progress == 1.0 and t.worker_id for t in tasks), tasks
        if check is not None:
            await check(rt, app, ev)
    finally:
        await rt.stop()


def _system(rt, ev, msg):
    return [e for e in rt.audit.tail(0, 100_000) if e.kind == AuditKind.SYSTEM and e.event_id == ev.id
            and e.payload.get("msg") == msg]


async def _review(rt, app, ev):
    """POST /api/events/{id}/review (主管已讀報告): LAN needs the PIN; pending -> done (by the session); 409 when none is
    pending."""
    url = f"/api/events/{ev.id}/review"
    async with _client(app, LAN) as phone, _client(app) as local:
        assert (await phone.post(url, json={"note": "x"})).status_code == 401           # LAN without the PIN
        assert rt.state_machine.get(ev.id).review.status == "pending"
        r = await phone.post(url, json={"note": "現場確認冷卻水正常"}, headers={"X-Supervisor-Pin": PIN})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == ev.id and body["review"]["status"] == "done"
        assert body["review"]["by"] == f"supervisor@{LAN[0]}" and body["review"]["done_at"]
        assert body["repaired_at"] and body["recovered_at"] and body["review"]["requested_at"]
        assert (await local.post(url)).status_code == 409                                # nothing pending now
        assert (await local.post("/api/events/EV-NOPE/review")).status_code == 404
        snap = (await local.get("/api/snapshot")).json()
        assert next(e for e in snap["events"] if e["id"] == ev.id)["review"]["status"] == "done"
        health = (await local.get("/api/health")).json()
        assert health["repair_fast_s"] == FAST_S and health["recover_fast_s"] == RECOVER_S
    done = _system(rt, ev, "主管已讀報告")
    assert len(done) == 1 and done[0].actor == "supervisor" and done[0].payload["note"] == "現場確認冷卻水正常"
    assert done[0].seq > _system(rt, ev, "異常報告已產生")[0].seq
    rows = rt.audit.tail(0, 100_000)
    assert not [e for e in rows if e.event_id == ev.id and e.payload.get("msg") in ("請主管覆核", "主管覆核")]


@pytest.mark.parametrize("speed", [10.0, 60.0])
async def test_open_mill_overheat_repair_then_ramp_then_green_then_report(tmp_path, speed):
    async def check(rt, app, ev):
        assert "A 線大開煉機 膠溫回到" in ev.outcome and "（維修完成 " in ev.outcome
        temp = rt.sim.machine("A-BIGOPEN").last_reading.temp_c
        assert abs(temp - 100.0) < 1.5, temp                    # back at nominal after the ramp
        rows = rt.audit.tail(0, 100_000)
        fixed = _system(rt, ev, "維修完成，故障排除")
        back = _system(rt, ev, "已恢復正常")
        report = _system(rt, ev, "異常報告已產生")
        assert len(fixed) == 1 and fixed[0].payload["machine_id"] == "A-BIGOPEN"
        assert len(back) == 1 and back[0].payload["machine_id"] == "A-BIGOPEN" and back[0].payload["value"] == 100.0
        assert len(report) == 1
        decided = [e for e in rows if e.kind == AuditKind.APPROVAL_DECISION and e.event_id == ev.id]
        closed = [e for e in rows if e.kind == AuditKind.STATE_TRANSITION and e.event_id == ev.id
                  and e.payload.get("to") == EventState.CLOSED.value]
        # 核准 → 維修完成 → 已恢復正常 → 結案 → 異常報告
        assert decided and decided[0].seq < fixed[0].seq < back[0].seq < closed[0].seq < report[0].seq
        assert any(e.kind == AuditKind.TASK and e.event_id == ev.id and e.payload.get("change") == "done" for e in rows)
        if speed == 10.0:
            await _review(rt, app, ev)
    await _fast_flow(tmp_path, "open_mill_overheat", speed, "A-BIGOPEN", check)


async def test_concurrent_3_b_open_fast_repair(tmp_path):
    async def check(rt, app, ev):
        assert "B 線開煉機 膠溫回到" in ev.outcome
        assert {e.machine_id for e in rt.events()} >= {"CURE-03", "B-OPEN", "CURE-10"}
        # 10-22 稽核 (older build): the mill closed before its 維修完成 and its report's 結果 had no reading
        assert ev.repaired_at <= ev.recovered_at <= ev.closed_at
        for _ in range(100):
            if rt.state_machine.get(ev.id).ai_report is not None:
                break
            await asyncio.sleep(0.05)
        last = rt.state_machine.get(ev.id).ai_report.text.splitlines()[-1]
        assert last.startswith("結果：B 線開煉機膠溫回到 ") and last.endswith(" 恢復正常）"), last
    await _fast_flow(tmp_path, "concurrent_3", 5.0, "B-OPEN", check)


# ------------------------------------------------------------------ concurrency: another event's long model call
class _SlowFirstPlan:
    """The real (mock) commander, except that the first plan for `machine` takes `slow_s` wall seconds (a real model's
    long call). Like any model call it holds the plant clock meanwhile (StateMachine.compute_inflight)."""

    def __init__(self, inner, machine: str, slow_s: float):
        self.inner, self.machine, self.slow_s = inner, machine, slow_s
        self.started = asyncio.Event()
        self.ended: float | None = None

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def plan(self, req, on_alert=None):
        if req.anomaly.machine_id == self.machine and not self.started.is_set():
            self.started.set()
            await asyncio.sleep(self.slow_s)
            self.ended = time.monotonic()
        return await self.inner.plan(req, on_alert=on_alert)


async def test_approved_repair_and_ramp_run_on_the_wall_clock_while_another_event_plans_for_20_s(tmp_path):
    """3 件同時異常 on stage: one event is approved while another's commander call holds the plant clock (a real model
    takes 10–20 s). The approved event goes approve → 維修完成 in ≈ repair_fast_s and 維修完成 → 綠燈 in ≈ recover_fast_s
    (+≤ 2 s) all the same: the repair, the ramp, VERIFY and the close all run on the wall clock (default settings)."""
    s = _settings(tmp_path, fast_s=Settings.model_fields["repair_fast_s"].default,
                  recover_s=Settings.model_fields["recover_fast_s"].default)
    assert (s.repair_fast_s, s.recover_fast_s) == (12.0, 4.0)
    rt = Runtime(s, allow_fallback=False)
    app = create_app(rt)
    await rt.start()
    try:
        await _kb_ready(rt)
        await rt.load_scenario(ScenarioName.NORMAL)
        rt.set_speed(60.0)
        a, b = [p for p in CURE_PRESS_IDS if curing(rt.sim, p)][:2]
        slow = _SlowFirstPlan(rt.state_machine.commander, b, slow_s=25.0)
        rt.state_machine.commander = slow
        steam = {"target_c": 122.5}
        rt.inject_fault(FaultSpec(id=f"F-T-{a}", machine_id=a, kind=FaultKind.STEAM_PRESSURE_DROP, params=steam))
        async with _client(app) as local:
            card = None
            t0 = time.monotonic()
            while card is None and time.monotonic() - t0 < 60:
                card = next((x for x in (await local.get("/api/approvals", params={"status": "pending"})).json()
                             if rt.state_machine.get(x["event_id"]).machine_id == a), None)
                await asyncio.sleep(0.1)
            assert card is not None, "no card for the first press"
            rt.inject_fault(FaultSpec(id=f"F-T-{b}", machine_id=b, kind=FaultKind.STEAM_PRESSURE_DROP, params=steam))
            await asyncio.wait_for(slow.started.wait(), 60)            # the other event's long model call is on
            assert rt.compute_busy()
            await _approve(local, card)
            approved = time.monotonic()
            ev = rt.state_machine.get(card["event_id"])
            repaired = warning = green = None
            busy = []
            while green is None and time.monotonic() - approved < 40:
                tile = next(t for t in (await local.get("/api/machines")).json() if t["machine_id"] == a)
                now = time.monotonic()
                if repaired is None and ev.repaired_at is not None:
                    repaired = now
                    busy.append(rt.compute_busy())
                if repaired is not None and warning is None and tile["status"] == "warning":
                    warning = now
                if repaired is not None and tile["status"] == "running" and ev.state is EventState.CLOSED:
                    green = now
                    busy.append(rt.compute_busy())
                await asyncio.sleep(0.05)
        assert green is not None, (ev.state, ev.outcome, [t.text for t in ev.timeline[-5:]])
        assert slow.ended is None and all(busy), "the other event's model call was not running the whole time"
        assert s.repair_fast_s - 0.5 <= repaired - approved <= s.repair_fast_s + 1.5, repaired - approved
        assert warning is not None and warning - repaired <= 0.6
        assert s.recover_fast_s - 0.5 <= green - repaired <= s.recover_fast_s + 2.0, green - repaired
        assert ev.outcome.startswith("已修復：") and ev.recovered_at is not None and ev.review.status == "pending"
        other = rt.state_machine.open_event_for(b)
        assert other is not None and other.state is EventState.PLAN             # still waiting for its model
    finally:
        await rt.stop()


# ------------------------------------------------------------------ never before the supervisor's decision
def _step(tool: ToolName, args: dict | None = None, risk: RiskLevel | None = None, state: str = "ok") -> StepRun:
    gate = None if risk is None else GateDecision(step_id="S", tool=tool, risk=risk, checks={}, verdict=(
        GateVerdict.ALLOW if risk is RiskLevel.R1_NOTIFY else GateVerdict.NEEDS_APPROVAL))
    return StepRun(step=PlanStep(id="S", tool=tool, args=args or {}, purpose="x"), state=state, gate=gate)


MAINT = {"kind": "maintenance", "machine_id": "CURE-02", "teams": ["maintenance"], "message": "x"}


def _awaits(state: EventState, steps: list[StepRun], prior: list[StepRun] = (), approved: bool = False) -> bool:
    ev = SimpleNamespace(id="EV-T", state=state, steps=list(steps), prior_rounds=[SimpleNamespace(steps=list(prior))])
    rt = SimpleNamespace(_event_obj=lambda eid: ev, state_machine=SimpleNamespace(),
                         approvals=SimpleNamespace(all=lambda: [SimpleNamespace(event_id="EV-T", status=(
                             ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED))]))
    return Runtime._awaits_decision(rt, "EV-T")


def test_a_stopped_event_with_r2_r3_in_its_plan_never_repairs_without_a_decision():
    r1 = _step(ToolName.NOTIFY_TEAMS, MAINT, RiskLevel.R1_NOTIFY)
    # steam_low on an idle press: the solve was refused, so the confirm / cure time were never gated; it safe-stopped
    never_gated = [_step(ToolName.CONFIRM_SCHEDULE, state="skipped"), _step(ToolName.SET_CURE_TIME, state="skipped")]
    assert _awaits(EventState.SAFE_STOP, [r1, *never_gated])
    assert _awaits(EventState.SAFE_STOP, [r1], prior=never_gated)                    # an earlier round had them
    assert _awaits(EventState.SAFE_STOP, [r1, _step(ToolName.NOTIFY_TEAMS, {"kind": "staff_move"}, state="skipped")])
    assert _awaits(EventState.SAFE_STOP, [r1, _step(ToolName.CONFIRM_SCHEDULE, risk=RiskLevel.R2_CHANGE,
                                                    state="rejected")])
    assert not _awaits(EventState.SAFE_STOP, [r1, *never_gated], approved=True)    # it was approved before it stopped
    assert not _awaits(EventState.SAFE_STOP, [r1])                                  # a plan of R1 writes only
    assert _awaits(EventState.EXECUTE, [r1, _step(ToolName.CONFIRM_SCHEDULE, risk=RiskLevel.R2_CHANGE,
                                                  state="awaiting_approval")])      # R1 before the card
    assert _awaits(EventState.EXECUTE, [r1, *never_gated])                          # its R1 ran, the rest never gated
    assert not _awaits(EventState.EXECUTE, [r1])
    # VERIFY / CLOSED with no card: the plan legitimately needed none (e.g. a no-op confirm dropped) — repair
    noop = _step(ToolName.CONFIRM_SCHEDULE, risk=RiskLevel.R2_CHANGE, state="skipped")
    assert not _awaits(EventState.VERIFY, [r1, noop]) and not _awaits(EventState.CLOSED, [r1, noop])
    for st in (EventState.PLAN, EventState.GATE, EventState.AWAIT_APPROVAL, EventState.REPLAN):
        assert _awaits(st, [r1], approved=True)


async def test_steam_low_on_an_idle_press_stops_with_no_card_and_its_repair_never_starts(tmp_path):
    rt = Runtime(_settings(tmp_path, fast_s=1.0), allow_fallback=False, run_loop=False)
    await rt.start()
    try:
        await rt.load_scenario(ScenarioName.NORMAL)
        idle = next(p for p in CURE_PRESS_IDS if rt.sim.machine(p).status is MachineStatus.IDLE)
        rt.inject_fault(FaultSpec(id="F-IDLE", machine_id=idle, kind=FaultKind.STEAM_PRESSURE_DROP,
                                  params={"target_c": 122.5}))
        ev = None
        for _ in range(3000):
            await rt.tick(1.0, publish=False)
            ev = next((e for e in rt.events() if e.machine_id == idle), None)
            if ev is not None and ev.state in (EventState.SAFE_STOP, EventState.CLOSED, EventState.AWAIT_APPROVAL):
                break
        assert ev is not None and ev.state is EventState.SAFE_STOP and not ev.approvals, ev.state
        for _ in range(40):                                  # 2 wall s: twice repair_fast_s
            await rt.tick(1.0, publish=False)
            await asyncio.sleep(0.05)
        tasks = [t for t in rt.sim.world.tasks.for_event(ev.id) if t.kind in REPAIR_KINDS]
        assert tasks and all(t.status == "待派" and t.worker_id is None and t.progress == 0.0 for t in tasks), tasks
        assert ev.repaired_at is None and any(f.machine_id == idle for f in rt.sim.faults())
    finally:
        await rt.stop()


# ------------------------------------------------------------------ the task board on its own
def _maintenance(board, now, eid="EV-T"):
    return board.dispatch(TaskRequest(event_id=eid, approval_id="AP-T", step_id="S3", event_kind="overheat",
                                      tool="notify_teams", args={"kind": "maintenance", "machine_id": "A-BIGOPEN",
                                                                 "teams": ["maintenance"], "message": "檢修冷卻水路"}),
                          now)


def _factory() -> Factory:
    f = Factory(seed=7, scenario=ScenarioName.OPEN_MILL_OVERHEAT)
    for _ in range(60):
        f.step(1.0)
    return f


def test_fast_task_is_assigned_at_once_and_done_on_the_wall_clock():
    f = _factory()
    board = f.world.tasks
    board.fast_s, board.speed = 3.0, lambda: 10.0
    (t,) = _maintenance(board, f.now())
    assert t.kind == "檢修" and t.status == "前往中" and t.worker_id                    # assigned at once
    assert abs(t.walk_min + t.work_min - 0.5) < 0.06                                   # 3 wall s at 10x = 30 factory s
    assert board.advance_fast(f.now(), 1.0)                                            # still open
    v = next(x for x in board.view().items if x.id == t.id)
    assert v.status == "執行中" and 0.3 < v.progress < 0.5
    assert not board.advance_fast(f.now(), 1.8)                                        # 2.8 s: done within 3 s
    v = next(x for x in board.view().items if x.id == t.id)
    assert v.status == "完成" and v.progress == 1.0 and v.done_at is not None


def test_a_later_round_does_not_send_the_crew_twice():
    """A stale schedule re-solved by code (or a replan) runs the plan's R1 檢修 again: the crew already on it (or done
    with it) for this event stays the one task, so the fix is not pushed back another repair_fast_s."""
    f = _factory()
    board = f.world.tasks
    board.fast_s, board.speed = 12.0, lambda: 60.0
    (t,) = _maintenance(board, f.now())
    board.advance_fast(f.now(), 5.0)
    assert _maintenance(board, f.now()) == []
    board.advance_fast(f.now(), 8.0)
    assert next(x for x in board.view().items if x.id == t.id).status == "完成"
    assert _maintenance(board, f.now()) == []
    assert len(_maintenance(board, f.now(), eid="EV-OTHER")) == 1                      # another event: its own crew


def test_repair_fast_s_zero_keeps_the_real_durations():
    f = _factory()
    board = f.world.tasks                                                              # fast_s 0: the default
    (t,) = _maintenance(board, f.now())
    assert t.work_min == 30.0 and t.walk_min >= 1.0
    assert not board.advance_fast(f.now(), 60.0)
    for _ in range(10 * 60):                                                           # 10 factory minutes
        f.step(1.0)
    assert next(x for x in board.view().items if x.id == t.id).status != "完成"


async def test_runtime_hands_repair_fast_s_to_the_board(tmp_path):
    rt = Runtime(_settings(tmp_path, fast_s=0.0, recover_s=0.0), run_loop=False)
    await rt.start()
    try:
        await rt.tick(1.0, publish=False)
        assert rt.sim.world.tasks.fast_s == 0.0
        async with _client(create_app(rt)) as c:
            health = (await c.get("/api/health")).json()
            assert health["repair_fast_s"] == 0.0 and health["recover_fast_s"] == 0.0
    finally:
        await rt.stop()


def test_settings_defaults_and_env(monkeypatch):
    for k in ("REPAIR_FAST_S", "RECOVER_FAST_S", "MODEL_SLOTS"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=None)
    assert (s.repair_fast_s, s.recover_fast_s, s.model_slots) == (12.0, 4.0, 3)
    monkeypatch.setenv("REPAIR_FAST_S", "0")
    monkeypatch.setenv("RECOVER_FAST_S", "2.5")
    s = Settings(_env_file=None)
    assert (s.repair_fast_s, s.recover_fast_s) == (0.0, 2.5)


def test_recover_ramps_the_reading_back_to_nominal():
    f = Factory(seed=7, scenario=ScenarioName.OPEN_MILL_OVERHEAT)
    for _ in range(12 * 60):                                       # cooling water failed at +135 s: well over 100 °C
        f.step(1.0)
    hot = f.machine("A-BIGOPEN").last_reading
    assert hot.temp_c > 108 and hot.water_flow_lpm < 30
    f.clear_faults("A-BIGOPEN")
    f.begin_recovery("A-BIGOPEN")
    half = f.recover("A-BIGOPEN", 0.5)                            # halfway from where the fix found it
    assert abs(half.temp_c - (hot.temp_c + 100.0) / 2) < 0.05
    assert abs(half.water_flow_lpm - (hot.water_flow_lpm + 40.0) / 2) < 0.05
    assert f.machine("A-BIGOPEN").last_reading.temp_c == half.temp_c == f.history("A-BIGOPEN", 0)[0].temp_c
    back = f.recover("A-BIGOPEN", 1.0)
    assert back.temp_c == 100.0 and back.water_flow_lpm == 40.0
    assert f.recover("A-BIGOPEN", 1.0) is None                     # the ramp is over
    f.step(1.0)
    assert abs(f.machine("A-BIGOPEN").last_reading.temp_c - 100.0) < 0.5


def test_snap_to_nominal_is_the_ramp_at_once():
    f = Factory(seed=7, scenario=ScenarioName.OPEN_MILL_OVERHEAT)
    for _ in range(12 * 60):
        f.step(1.0)
    f.clear_faults("A-BIGOPEN")
    f.snap_to_nominal("A-BIGOPEN")
    st = f.machine("A-BIGOPEN")
    assert st.last_reading.temp_c == 100.0 and st.last_reading.water_flow_lpm == 40.0
