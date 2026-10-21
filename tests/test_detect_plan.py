"""Page 6 box 4: 「事件偵測：每分鐘比對計畫與實際，異常喚醒指揮官」."""
from datetime import datetime, timedelta

from warroom.agent.commander import MockCommander
from warroom.contracts import (
    AnomalyKind,
    CommanderRequest,
    MachineStatus,
    ScenarioName,
    Schedule,
    SemiFinishedPart,
    SemiFinishedProgress,
    SolverName,
    TireSize,
    ToolName,
)
from warroom.detect import StreamDetector
from warroom.detect.plan_check import PlanWatcher
from warroom.factory import TZ
from warroom.schedule import back_propagate
from warroom.sim import Factory

T0 = datetime(2026, 10, 3, 8, 0, tzinfo=TZ)


def _sched(jobs):
    return Schedule(id="S", created_at=T0, horizon_start=T0, horizon_end=T0 + timedelta(days=7), jobs=jobs,
                    solver=SolverName.HEURISTIC)


class _Store:
    def __init__(self, s):
        self.s = s

    def current(self):
        return type("V", (), {"schedule": self.s})()


def test_late_demold_raises_one_schedule_slip_per_job():
    f = Factory(seed=7, scenario=ScenarioName.NORMAL)
    pid, m = next((m.machine_id, m) for m in f.machines() if m.cure_job is not None)
    f.set_machine_status(pid, MachineStatus.STOPPED, "held")      # tire stays in the mold
    w = PlanWatcher()
    demold = m.cure_job.planned_demold_at
    assert [a for a in w.check(demold + timedelta(minutes=29), None, f.machines()) if a.machine_id == pid] == []
    slips = [a for a in w.check(demold + timedelta(minutes=31), None, f.machines()) if a.machine_id == pid]
    assert len(slips) == 1
    a = slips[0]
    assert a.kind is AnomalyKind.SCHEDULE_SLIP and a.value == 31.0 and "出模" in a.message
    assert all(x.machine_id != pid for x in w.check(demold + timedelta(minutes=90), None, f.machines()))


def test_late_mold_in_needs_press_empty_since_T():
    f = Factory(seed=7, scenario=ScenarioName.NORMAL)
    empty = [m for m in f.machines() if m.machine_id.startswith("CURE-")]
    m = empty[0].model_copy(update={"cure_job": None, "status": MachineStatus.IDLE})
    T = T0 + timedelta(hours=1)
    job = back_propagate("J-X", "#S-101", TireSize.S, m.machine_id, T)
    sched = _sched([job])
    w = PlanWatcher()
    # press busy at T, empty only from T+20 (normal change-over) -> no slip
    busy = empty[0]
    assert busy.cure_job is not None
    w.check(T, sched, [busy])
    assert w.check(T + timedelta(minutes=20), sched, [m]) == []
    assert w.check(T + timedelta(minutes=45), sched, [m]) == []
    # a fresh watcher that sees the press empty from before T flags it after the 30-min grace
    w2 = PlanWatcher()
    assert w2.check(T - timedelta(minutes=5), sched, [m]) == []
    assert w2.check(T + timedelta(minutes=25), sched, [m]) == []
    out = w2.check(T + timedelta(minutes=31), sched, [m])
    assert len(out) == 1 and "入模" in out[0].message and out[0].machine_id == m.machine_id
    # an open event on the press owns it; out of service is not a slip either
    w3 = PlanWatcher()
    w3.check(T - timedelta(minutes=5), sched, [m])
    assert w3.check(T + timedelta(minutes=40), sched, [m.model_copy(update={"active_event_id": "EV-1"})]) == []


def test_mold_in_done_before_the_schedule_took_effect_is_not_a_slip():
    """Page 11, round 2 approved late: v3 (solved at 02:31) moves a tire to press 3 that v2 had already loaded on
    press 1 at 04:00. Once v3 is in force, press 1 holding it is not 「偏離排程」 and press 3 standing empty at
    04:00 is not a late mold-in: both happened under v2. A mold loaded after v3 took effect still is."""
    from warroom.contracts import CureJobState
    f = Factory(seed=7, scenario=ScenarioName.NORMAL)
    presses = [m for m in f.machines() if m.machine_id.startswith("CURE-")]
    p1 = presses[0].machine_id
    empty = presses[1].model_copy(update={"cure_job": None, "status": MachineStatus.IDLE})
    T = T0 + timedelta(hours=2)
    v2 = _sched([back_propagate("J-311", "#A-311", TireSize.S, p1, T)])
    v3 = Schedule(id="S3", created_at=T0 - timedelta(minutes=90), horizon_start=T0, horizon_end=T0 + timedelta(days=7),
                  jobs=[back_propagate("J-311", "#A-311", TireSize.S, empty.machine_id, T)], solver=SolverName.CP_SAT)
    loaded = presses[0].model_copy(update={"cure_job": CureJobState(
        job_id="J-311", order_id="#A-311", size=TireSize.S, mold_id="M", mold_in_at=T, planned_demold_at=T + timedelta(hours=24),
        setpoint_c=128.0, cure_required_h=24.0, cure_elapsed_h=0.0)})
    idle1 = presses[0].model_copy(update={"cure_job": None, "status": MachineStatus.IDLE})
    w = PlanWatcher()
    assert w.check(T - timedelta(minutes=10), v2, [idle1, empty]) == []
    assert w.check(T + timedelta(minutes=5), v2, [loaded, empty]) == []
    # v3 takes effect at T+60: nothing it planned before then, nothing loaded before then, is a slip of v3
    assert w.check(T + timedelta(minutes=60), v3, [loaded, empty]) == []
    assert w.check(T + timedelta(minutes=120), v3, [loaded, empty]) == []
    # a mold loaded on press 1 after v3 took effect is off plan
    late = loaded.model_copy(update={"cure_job": loaded.cure_job.model_copy(
        update={"job_id": "J-999", "order_id": "#Z-1", "mold_in_at": T + timedelta(minutes=130)})})
    out = w.check(T + timedelta(minutes=140), v3, [late, empty])
    assert [a.id.split("-SLIP-")[1] for a in out] == ["off_plan"] and out[0].machine_id == p1


def test_wip_behind_back_propagated_T():
    T = T0 + timedelta(hours=9)       # 17:00
    parts = [SemiFinishedPart(part="A", label="A 胎體／胎面膠", status_text="", state="waiting", machine_id="A-BIGOPEN"),
             SemiFinishedPart(part="B", label="B 胎側膠", status_text="", state="in_progress", machine_id="B-FORM")]
    semi = SemiFinishedProgress(tire_label="大胎 #07", order_id="#L-301", size=TireSize.L, parts=parts,
                                planned_mold_in_at=T)
    w = PlanWatcher()
    # large tire: A feed at T-3h30 = 13:30 (+15 min grace)
    assert w.check(T - timedelta(minutes=200), None, [], semi) == []
    out = w.check(T - timedelta(minutes=194), None, [], semi)
    assert len(out) == 1 and out[0].machine_id == "A-BIGOPEN" and "13:30" in out[0].message
    # bonding at T-1h30 = 15:30: part B in alert -> slip
    semi2 = semi.model_copy(update={"parts": [parts[0], parts[1].model_copy(update={"state": "alert"})]})
    assert [a.machine_id for a in w.check(T - timedelta(minutes=74), None, [], semi2)] == ["A-BIGOPEN", "B-FORM"]


def test_detector_checks_plan_once_per_sim_minute_inside_ingest():
    f = Factory(seed=7, scenario=ScenarioName.NORMAL)
    pid = next(m.machine_id for m in f.machines() if m.cure_job is not None)
    demold = f.machine(pid).cure_job.planned_demold_at
    d = StreamDetector()
    d.attach_plan(store=_Store(_sched([])), sim=f)
    calls = []
    real = d.plan_watcher.check
    d.plan_watcher.check = lambda *a, **k: calls.append(a[0]) or real(*a, **k)
    for _ in range(125):
        d.ingest(f.step(1.0))
    assert 2 <= len(calls) <= 3                  # 125 sim seconds -> every 60 s
    # jump the plant past the planned de-mold with the press held: ingest returns the slip
    f.set_machine_status(pid, MachineStatus.STOPPED, "held")
    while f.now() < demold + timedelta(minutes=31):
        f.step(60.0)
    got = d.ingest(f.step(1.0))
    assert any(a.kind is AnomalyKind.SCHEDULE_SLIP and a.machine_id == pid for a in got)


def test_normal_plant_against_seeded_schedule_has_no_slips_for_a_day():
    from warroom.schedule import seed_schedule
    f = Factory(seed=7, scenario=ScenarioName.NORMAL)
    sched = seed_schedule(f.now(), f.orders(), f.shifts(), f.machines())
    f.attach_plan(_Store(sched))                 # the plant loads presses from the plan
    d = StreamDetector()
    d.attach_plan(store=_Store(sched), sim=f)
    slips = []
    for _ in range(24 * 60):
        f.step(60.0)
        slips += d.check_plan(f.now())
    assert slips == []


def test_plant_running_its_own_queue_is_flagged_off_plan():
    """A plant that ignores the plan (no attach_plan: it loads presses from its own queue) is caught:
    a press holding a mold the active schedule does not have on it is a SCHEDULE_SLIP 'off_plan'."""
    from warroom.schedule import seed_schedule
    f = Factory(seed=7, scenario=ScenarioName.NORMAL)
    sched = seed_schedule(f.now(), f.orders(), f.shifts(), f.machines())
    d = StreamDetector()
    d.attach_plan(store=_Store(sched), sim=f)
    slips = []
    for _ in range(6 * 60):
        f.step(60.0)
        slips += d.check_plan(f.now())
    off = [a for a in slips if a.id.endswith("off_plan")]
    assert off and all(a.kind is AnomalyKind.SCHEDULE_SLIP and "偏離排程" in a.message for a in off)
    assert len({a.machine_id for a in off}) == len(off)          # once per job


async def test_mock_commander_plans_a_reschedule_for_a_slip():
    f = Factory(seed=7, scenario=ScenarioName.NORMAL)
    pid, m = next((m.machine_id, m) for m in f.machines() if m.cure_job is not None)
    f.set_machine_status(pid, MachineStatus.STOPPED)
    a = PlanWatcher().check(m.cure_job.planned_demold_at + timedelta(hours=1), None, f.machines())[0]
    plan, _ = await MockCommander().plan(CommanderRequest(event_id="EV-1", anomaly=a, phase="plan", allowed_tools=list(ToolName)))
    tools = [s.tool for s in plan.steps]
    assert tools == [ToolName.QUERY_ORDERS_SHIFTS, ToolName.SOLVE_SCHEDULE, ToolName.CONFIRM_SCHEDULE,
                     ToolName.NOTIFY_TEAMS]
    assert plan.hypothesis.cause_code == "schedule_slip"


async def test_runtime_slip_wakes_commander_and_closes_after_approval(tmp_path):
    """Live path: Runtime tick -> detector plan check -> SCHEDULE_SLIP -> state machine (mock commander)
    -> reschedule proposal -> supervisor approves over REST -> event closes."""
    import httpx

    from warroom.api import Runtime, create_app
    from warroom.config import Settings
    from warroom.contracts import EventState

    rt = Runtime(Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched",
                          qdrant_path=tmp_path / "qdrant", scenario="normal", llm_mode="mock"),
                 allow_fallback=False, run_loop=False)
    await rt.start()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(rt)), base_url="http://test")
    try:
        sim = rt.c.sim
        pid, cj = next((m.machine_id, m.cure_job) for m in sim.machines() if m.cure_job is not None)
        sim.set_machine_status(pid, MachineStatus.STOPPED, "held")
        while sim.now() < cj.planned_demold_at + timedelta(minutes=30):    # fast-forward the plant
            sim.step(60.0)
        for _ in range(70):
            await rt.tick(1.0, publish=False)
        slip = next(a for a in rt.anomalies if a.kind is AnomalyKind.SCHEDULE_SLIP and a.machine_id == pid)
        assert "出模" in slip.message
        for _ in range(60):
            await rt.tick(1.0, publish=False)
            pend = (await client.get("/api/approvals", params={"status": "pending"})).json()
            if pend:
                break
        assert pend, "commander never asked for approval"
        req = pend[0]
        body = {"request_id": req["id"], "decision": "approve", "reason": "同意", "via": "phone"}
        if req.get("options"):
            body["option_id"] = req["options"][0]["id"]
        r = await client.post(f"/api/approvals/{req['id']}/decision", json=body)
        assert r.status_code == 200, r.text
        for _ in range(120):
            await rt.tick(1.0, publish=False)
        ev = next(e for e in (await client.get("/api/events")).json() if e["anomaly"]["id"] == slip.id)
        assert ev["state"] == EventState.CLOSED.value, ev["state"]
        assert (await client.get("/api/schedule")).json()["version"] >= 2
    finally:
        await client.aclose()
        await rt.stop()
