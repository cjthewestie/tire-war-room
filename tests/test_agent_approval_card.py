"""Approval cards carry only THIS event's own facts: the solver gaps its changes land in, the
staffing A/B that closes them, a filled 交期 row, and a '將做' line naming what is being approved.
Real Runtime, mock commander (no model needed)."""
from __future__ import annotations

import pytest

from warroom.api import Runtime
from warroom.config import Settings
from warroom.contracts import ApprovalDecision, ApprovalRequest, DecisionKind

pytestmark = pytest.mark.timeout(300)


async def _cards(tmp_path, scenario: str, minutes: int = 20) -> dict[str, list[ApprovalRequest]]:
    s = Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched",
                 qdrant_path=tmp_path / "qdrant", scenario=scenario, llm_mode="mock")
    rt = Runtime(s, allow_fallback=False, run_loop=False)
    await rt.start()
    rt._ingest_kb()
    seen: dict[str, ApprovalRequest] = {}
    out: dict[str, list[ApprovalRequest]] = {}
    try:
        for _ in range(minutes * 60):
            await rt.tick(1.0, publish=False)
            for p in rt.approvals.pending():
                if p.id in seen:
                    continue
                seen[p.id] = p
                ev = next(e for e in rt.events() if e.id == p.event_id)
                out.setdefault(ev.machine_id, []).append(p)
                rec = next((o.id for o in p.options if o.recommended), p.options[0].id)
                rt.approvals.decide(ApprovalDecision(request_id=p.id, decision=DecisionKind.APPROVE,
                                                     option_id=rec, reason="test", via="dashboard"))
            if out and len(out) >= 3:
                break
    finally:
        await rt.stop()
    return out


async def test_page2_mill_card_has_no_unrelated_gap(tmp_path):
    cards = await _cards(tmp_path, "open_mill_overheat")
    card = cards["A-BIGOPEN"][0]
    assert card.gaps == []                                   # a plant-wide night shortage is not this event's
    assert card.delivery_impact.startswith("不受影響")
    assert "大胎 #07" in card.delivery_impact
    assert any(d.startswith("將做：") and "放慢生料投入" in d for d in card.done)
    assert [o.label for o in card.options] == ["放慢生料投入"]


async def test_concurrent_cards_do_not_reuse_page11_staffing(tmp_path):
    cards = await _cards(tmp_path, "concurrent_3")
    for mid in ("CURE-03", "CURE-10", "B-OPEN"):
        card = cards[mid][0]
        assert not any("夜班" in g for g in card.gaps), (mid, card.gaps)
        assert not any("夜班" in o.label or "2 線" in o.label for o in card.options), (mid, card.options)
        assert card.delivery_impact, mid
        assert any(d.startswith("將做：") for d in card.done), (mid, card.done)
    assert any("延長硫化" in d for d in cards["CURE-03"][0].done)


async def test_page11_round1_says_what_is_approved(tmp_path):
    cards = await _cards(tmp_path, "press7_temp_drop", minutes=5)
    card = cards["CURE-07"][0]
    todo = next(d for d in card.done if d.startswith("將做："))
    assert "延長硫化 36.5 h（+8.5 h）" in todo
    assert "#A-311 改" in todo
    assert card.gaps == ["10/03 夜班缺 1 人"]               # #A-311's new mold-in lands in that night shift
    assert [o.label for o in card.options] == ["夜班加班", "從 2 線調 1 人"]
    assert card.delivery_impact == "不受影響"
