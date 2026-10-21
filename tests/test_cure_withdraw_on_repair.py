"""#92 review: after the press is repaired, the event's pending 「設定硫化時間 28.5 h（+4.5 h）」 and its 宣達 were computed for
the cold press (122 °C) and would over-cure it. on_repaired withdraws them and posts a recheck task with the formula
recomputed from the reading now (code, no model), audited."""
from __future__ import annotations

from tests.agent_fakes import T0
from tests.test_agent import make_sm
from tests.test_agent_page11 import page11_anomaly
from warroom.agent.narrate import fmt_num
from warroom.clock import SimClock
from warroom.contracts import AuditKind, ScenarioName
from warroom.sim import Factory
from warroom.sim.work import TaskRequest


def test_repair_withdraws_the_cure_extension_and_posts_a_recheck(tmp_path):
    _clock, audit, _sim, _store, _tools, _approvals, sm = make_sm(tmp_path)
    plant = Factory(clock=SimClock(start=T0, manual=True), scenario=ScenarioName.PRESS7_TEMP_DROP)
    for _ in range(60):
        plant.step(1.0)
    sm.sim = plant
    board = plant.world.tasks
    ev = sm._open_event(page11_anomaly())
    now = plant.clock.now()
    base = {"event_id": ev.id, "approval_id": "AP-1", "event_kind": "temp_drop"}
    board.dispatch(TaskRequest(step_id="S4b", tool="set_cure_time",
                               args={"press_id": "CURE-07", "total_cure_h": 28.5, "reason": "x"}, **base), now)
    board.dispatch(TaskRequest(step_id="S5", tool="notify_teams",
                               args={"teams": ["curing", "body"], "kind": "info", "machine_id": "CURE-07",
                                     "message": "7 號機延長硫化至 28.5 h（+4.5 h），已依新排程調整下一模"}, **base), now)
    before = board.for_event(ev.id)
    assert {t.kind for t in before} == {"設定硫化時間", "宣達"}

    sm.on_repaired(ev.id, "CURE-07", now)

    after = board.for_event(ev.id)
    assert [t.kind for t in after] == ["設定硫化時間"] and after[0].id not in {t.id for t in before}
    assert after[0].title.startswith("7 號機：硫化機已回溫，請依公式重新確認硫化時數（現在 ")
    rec = [e.payload for e in audit.tail(0, 0) if e.kind is AuditKind.SYSTEM and "cure_extension_withdrawn" in e.payload]
    assert len(rec) == 1 and set(rec[0]["cure_extension_withdrawn"]) == {t.id for t in before}
    r = rec[0]["recomputed"]
    live = plant.machine("CURE-07")
    assert r["args"]["actual_temp_c"] == max(live.last_reading.temp_c, 120.0)
    assert r["args"]["elapsed_h"] == live.cure_job.cure_elapsed_h
    assert f"公式 {fmt_num(r['total_cure_h'])} h" in after[0].title and r["total_cure_h"] < 28.5   # warm again: less than +4.5 h
    assert any("撤回尚未完成的延長硫化設定" in t.text for t in ev.timeline)


def test_done_cure_setting_is_not_withdrawn(tmp_path):
    _clock, audit, _sim, _store, _tools, _approvals, sm = make_sm(tmp_path)
    plant = Factory(clock=SimClock(start=T0, manual=True), scenario=ScenarioName.PRESS7_TEMP_DROP)
    sm.sim = plant
    ev = sm._open_event(page11_anomaly())
    sm.on_repaired(ev.id, "CURE-07", plant.clock.now())     # nothing pending: no recheck task, no audit row
    assert plant.world.tasks.for_event(ev.id) == []
    assert not [e for e in audit.tail(0, 0) if "cure_extension_withdrawn" in e.payload]
