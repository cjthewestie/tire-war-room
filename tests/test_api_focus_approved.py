"""③ keeps approved R2/R3 writes as ✓ 已核准 (not ✓ 已自動), and ⑥ keeps the earlier plan rounds."""
import asyncio

from warroom.contracts import ApprovalDecision, DecisionKind, RiskLevel, ScenarioName

from tests.test_api import _real_runtime


async def _run(rt, minutes, pick=None):
    for _ in range(minutes * 60):
        await rt.tick(1.0, publish=False)
        await asyncio.sleep(0)
        for req in rt.approvals.pending():
            opt = pick(req) if pick else None
            rt.approvals.decide(ApprovalDecision(
                request_id=req.id, decision=DecisionKind.MODIFY if opt else DecisionKind.APPROVE,
                option_id=opt.id if opt else None, decided_by="主管", decided_at=rt.now()))
    await rt.wait_events(5)


async def test_page2_approved_slow_feed_stays_checked(tmp_path):
    rt = _real_runtime(tmp_path, ScenarioName.OPEN_MILL_OVERHEAT.value)
    await rt.start()
    try:
        await _run(rt, 30)
        f = rt.focus_panel()
        assert f is not None and f.machine_id == "A-BIGOPEN"
        assert not f.pending
        assert [a.label for a in f.approved] == ["放慢生料投入"], f.approved
        assert f.approved[0].decided_by == "主管"
        assert all("放慢" not in a for a in f.auto_done), f.auto_done
        ev = rt.event(f.event_id) if hasattr(rt, "event") else next(e for e in rt.events() if e.id == f.event_id)
        r1 = [sr for sr in ev.steps if sr.gate and sr.gate.risk is RiskLevel.R1_NOTIFY]
        assert r1 and all(sr.step.purpose.replace("（自動）", "") in " ".join(f.auto_done) for sr in r1)
    finally:
        await rt.stop()


async def test_page11_round1_steps_kept_and_overtime_shown(tmp_path):
    rt = _real_runtime(tmp_path, ScenarioName.PRESS7_TEMP_DROP.value)
    await rt.start()
    try:
        await _run(rt, 25, pick=lambda req: next((o for o in req.options if "夜班" in o.label), None))
        ev = next(e for e in rt.events() if e.machine_id == "CURE-07")
        assert ev.prior_rounds, "round-1 steps were dropped on replan"
        r1 = ev.prior_rounds[0]
        assert r1.round == 1 and r1.hypothesis_id == "H1"
        tools = [sr.step.tool.value for sr in r1.steps]
        assert tools[:3] == ["cure_formula", "query_orders_shifts", "solve_schedule"], tools
        assert set(r1.steps[2].step.depends_on) == {"S1", "S2"}
        f = rt.focus_panel(ev)
        labels = [a.label for a in f.approved]
        assert "夜班加班" in labels, labels
        assert not any("從 2 線" in x for x in labels), labels
        assert any("28.5" in x for x in labels), labels
    finally:
        await rt.stop()


async def test_concurrent3_round2_card_is_not_marked_stale(tmp_path):
    # concurrent_3: 10 號's round-1 confirm went stale (3 號's version won), its round-2 card put v4 in force. ③ showed
    # both cards 「（✗ 未生效：排程已被另一事件更新）」 beside 「已生效 第 4 版」: the round-2 card was matched to round 1
    rt = _real_runtime(tmp_path, ScenarioName.CONCURRENT_3.value)
    await rt.start()
    try:
        for _ in range(5 * 60):
            await rt.tick(1.0, publish=False)
            await asyncio.sleep(0)
            for req in rt.approvals.pending():
                rt.approvals.decide(ApprovalDecision(
                    request_id=req.id, decision=DecisionKind.APPROVE, option_id=req.options[0].id if req.options else None,
                    decided_by="主管", decided_at=rt.now()))
        ev = next(e for e in rt.events() if e.machine_id == "CURE-10")
        rounds = [r.steps for r in ev.prior_rounds] + [ev.steps]
        confirms = [[sr.state for sr in rnd if sr.step.tool.value == "confirm_schedule"] for rnd in rounds]
        assert confirms == [["failed"], ["ok"]], confirms
        active = rt.c.store.current().version
        f = rt.focus_panel(ev)
        by = {}
        for a in f.approved:
            by.setdefault(a.approval_id, []).append(a.label)
        # the re-approval after the stale confirm is v2 of the same card (AP-…-1v2, group B 10-21), formerly round 2
        r1 = by.get(f"AP-{ev.id}-1", [])
        r2 = by.get(f"AP-{ev.id}-1v2") or by.get(f"AP-{ev.id}-2", [])
        assert len(r1) == 1 and "未生效" in r1[0], by
        assert r2 and not any("未生效" in x for x in r2), by
        assert any(f"排程 v{active}" in x for x in r2), (active, by)
    finally:
        await rt.stop()
