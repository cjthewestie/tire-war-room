"""Tiered memory (CJ, 2026-10-22: 「加每日摘要」「那越來越多的記憶每天的記憶要怎麼管理 會壓縮成摘要嗎還是有什麼方式」).

Daily summaries at the 一日循環 wrap and at a real date boundary, built from the audit by code and phrased by one model
call only when every number is grounded; weekly / monthly roll-ups; a rebuild from the audit that is idempotent; the
memory_search no-answer gate; the commander's machine_memory stays under its size cap."""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from scripts.rebuild_memory import rebuild
from tests.test_plan_talk import Plant, _rt
from warroom.contracts import AuditKind, ScenarioName
from warroom.data import machine_memory as MM
from warroom.data import memory_log as ML
from warroom.data.memory_notes import MemoryNotes
from warroom.factory import TZ


def _event_rows(audit, eid="EV-20261021-090000-CURE-07"):
    audit.append(AuditKind.ANOMALY, "detector", {"machine_id": "CURE-07", "message": "7 號 128 → 122°C，預測穩定"},
                 event_id=eid)
    audit.append(AuditKind.APPROVAL_REQUEST, "state_machine", {"id": f"AP-{eid}-1", "version": 1,
                                                               "reason": "假設 H1：蒸汽壓不足，溫度會穩定", "title": "7 號機溫度下降"},
                 event_id=eid)
    audit.append(AuditKind.APPROVAL_DECISION, "supervisor", {"decision": "approve", "request_id": f"AP-{eid}-1",
                                                             "option_id": "B"}, event_id=eid)
    audit.append(AuditKind.SYSTEM, "verifier", {"hypothesis": "H1", "verdict": "rejected"}, event_id=eid)
    audit.append(AuditKind.STATE_TRANSITION, "state_machine", {"from": "verify", "to": "closed"}, event_id=eid)


# ---------------------------------------------------------------- daily summary triggers
@pytest.mark.timeout(300)
async def test_daily_summary_is_written_at_the_day_loop_wrap(tmp_path):
    rt = _rt(tmp_path, ScenarioName.DAY_LOOP.value)
    await rt.start()
    try:
        await rt.tick(1.0, publish=False)                  # 00:00 snapshot of the day
        day = rt.clock.now().astimezone(TZ).date().isoformat()
        _event_rows(rt.audit)
        await rt._wrap_day()
        await rt.memory_desk.drain()
        doc = rt.memory_desk.files.read("daily", f"{day}-loop1")
        assert doc is not None and doc["label"] == f"{day}（循環 1）" and doc["source"] == "code"   # mock mode: code
        assert doc["facts"]["tasks"] >= 1 and len(doc["sentences"]) <= ML.MAX_DAILY_SENTENCES
        ev = next(e for e in doc["facts"]["events"] if e["id"] == "EV-20261021-090000-CURE-07")
        assert ev["hypotheses"] == ["H1 蒸汽壓不足，溫度會穩定（推翻）"] and ev["approvals"] == ["v1 核准"]
        assert "CURE-07" in doc["machines"]
        assert (tmp_path / "memory" / "daily" / f"{day}-loop1.md").read_text(encoding="utf-8").startswith("# 每日摘要")
        rows = [e for e in rt.audit.tail(0, 0) if e.actor == "memory"]
        assert rows and rows[-1].kind == AuditKind.SYSTEM and rows[-1].payload["name"] == f"{day}-loop1"
    finally:
        await rt.stop()


@pytest.mark.timeout(300)
async def test_daily_summary_is_written_at_the_date_boundary(tmp_path):
    rt = _rt(tmp_path, ScenarioName.NORMAL.value)
    await rt.start()
    try:
        desk = rt.memory_desk
        day = rt.clock.now().astimezone(TZ).date()
        desk.on_tick()                                          # first look: remembers the date, writes nothing
        _event_rows(rt.audit, f"EV-{day:%Y%m%d}-090000-CURE-07")
        now = rt.clock.now().astimezone(TZ)
        rt.clock.advance((now.replace(hour=23, minute=59, second=59) - now).total_seconds() + 2)
        desk.on_tick()
        await desk.drain()
        doc = desk.files.read("daily", day.isoformat())
        assert doc is not None and doc["loop"] is None and doc["facts"]["tasks"] >= 1
        assert desk.files.read("daily", (day + timedelta(days=1)).isoformat()) is None
    finally:
        await rt.stop()


# ---------------------------------------------------------------- grounding
def _facts():
    rows = []

    class A:                                                   # a tiny audit stand-in: rows as the file has them
        seq = 0

        def append(self, kind, actor, payload, event_id=None):
            A.seq += 1
            rows.append({"seq": A.seq, "ts": "2026-10-21T09:00:00+08:00", "kind": kind.value, "actor": actor,
                         "event_id": event_id, "payload": payload})
    _event_rows(A())
    return ML.daily_facts(rows, "2026-10-21", None)


def test_the_model_summary_is_kept_only_when_every_number_is_in_the_facts():
    f = _facts()
    assert ML.check_sentences("2026-10-21 處理 1 件任務，7 號機的 H1 被推翻，主管核准 v1 後結案。", f)
    assert ML.check_sentences("處理 3 件任務。", f) is None                     # 3 is not in the facts
    assert ML.check_sentences("處理 1 件任務，1 號機被核准。", f) is None         # press 1 is not in the facts (real run 10-22)
    assert ML.check_sentences("。".join(["一件事"] * 9) + "。", f) is None      # more than 8 sentences
    assert all(not __import__("warroom.agent.main_agent", fromlist=["x"]).ungrounded(s, ML.facts_text(f))
               for s in ML.code_sentences(f))                                   # the code's own summary is grounded


@pytest.mark.timeout(300)
async def test_an_ungrounded_model_summary_falls_back_to_the_code_facts(tmp_path, monkeypatch):
    rt = _rt(tmp_path, ScenarioName.NORMAL.value)
    await rt.start()
    try:
        desk = rt.memory_desk
        day = rt.clock.now().astimezone(TZ).date().isoformat()
        _event_rows(rt.audit, f"EV-{day.replace('-', '')}-090000-CURE-07")

        async def model(f):
            return "今天處理了 42 件任務，非常順利。"
        monkeypatch.setattr(desk, "_complete", model)
        doc = await desk.summarize(day, None)
        assert doc["source"] == "code" and "42" not in "".join(doc["sentences"])
        row = [e for e in rt.audit.tail(0, 0) if e.actor == "memory"][-1]
        assert row.payload["model_error"].startswith("ungrounded")

        async def good(f):
            return f"{day} 處理 1 件任務，7 號機的 H1 被推翻，主管核准 v1 後結案。"
        monkeypatch.setattr(desk, "_complete", good)
        doc = await desk.summarize(day, None)
        assert doc["source"] == "model" and doc["sentences"][0].startswith(day)
    finally:
        await rt.stop()


# ---------------------------------------------------------------- roll-up and rebuild
def _daily(files, day, machine_line=None):
    f = ML.daily_facts([], day, None)
    if machine_line:
        f["machines"] = {"CURE-07": f"7 號機：{machine_line}"}
    files.write("daily", files.daily_doc(f, ML.code_sentences(f), "code", f"{day}T23:59:59+08:00"))


def test_seven_dailies_roll_up_into_a_week(tmp_path):
    files = ML.MemoryFiles(tmp_path / "memory")
    mon = date(2026, 10, 12)                                   # ISO week 42, Monday
    for i in range(6):
        _daily(files, (mon + timedelta(days=i)).isoformat(), f"第 {i} 天")
    assert files.roll_up("x") == [] and not files.names("weekly")              # 6 of 7 and the week is open
    _daily(files, (mon + timedelta(days=6)).isoformat(), "第 6 天")
    assert files.roll_up("x") == ["2026-W42", "2026-10"]
    w = files.read("weekly", "2026-W42")
    assert len(w["parts"]) == 7 and len(w["sentences"]) <= ML.MAX_WEEKLY_SENTENCES
    assert w["machines"]["CURE-07"].startswith("7 號機：2026-10-12 第 0 天")
    assert files.read("monthly", "2026-10")["parts"] == ["2026-W42"]
    assert (tmp_path / "memory" / "weekly" / "2026-W42.md").exists()
    assert files.roll_up("y") == []                                             # nothing changed: nothing rewritten
    assert files.machine_lines("CURE-07", ("weekly", "monthly"))[0]["level"] == "weekly"


def _tree(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and p.suffix in (".md", ".json")}


@pytest.mark.timeout(300)
async def test_rebuild_from_the_audit_is_idempotent(tmp_path):
    async with Plant(_rt(tmp_path, ScenarioName.PRESS7_TEMP_DROP.value)) as p:
        await p.until(lambda: p.pending())
    audit = tmp_path / "audit.jsonl"
    first = rebuild(audit, at="fixed")
    tree, obs = _tree(tmp_path / "memory"), ML.ObservationStore(tmp_path / "observations.sqlite").all()
    second = rebuild(audit, at="fixed")
    assert _tree(tmp_path / "memory") == tree and ML.ObservationStore(tmp_path / "observations.sqlite").all() == obs
    assert first["observations"] == second["observations"] > 0 and second["daily_written"] == 0
    assert audit.exists() and first["rows"] == second["rows"]                   # the audit is only read


# ---------------------------------------------------------------- search and size
def test_memory_search_answers_nothing_when_the_match_is_weak(tmp_path):
    files = ML.MemoryFiles(tmp_path / "memory")
    _daily(files, "2026-10-12", "疏水閥卡住，換了疏水閥後恢復")
    idx = ML.MemoryIndex(None)
    idx.set_docs(ML.summary_docs(files))
    hit = idx.search("7 號機 疏水閥卡住")
    assert hit["found"] and hit["hits"][0]["id"] == "daily:2026-10-12"
    miss = idx.search("天氣預報颱風")
    assert miss == {"found": False, "hits": [], "best_score": miss["best_score"],
                    "note": "記憶裡找不到相關內容（分數太低，不算查到）"} and miss["best_score"] < ML.MIN_SCORE


def test_commander_machine_memory_stays_under_its_cap(tmp_path):
    notes = MemoryNotes(tmp_path / "n.json")
    from datetime import datetime
    for i in range(9):
        notes.add("CURE-07", f"很長的筆記 {i} " + "蒸汽" * 240, "admin:sup", datetime(2026, 10, 3, 8, i, tzinfo=TZ))
    files = ML.MemoryFiles(tmp_path / "memory")
    for i in range(6):
        _daily(files, f"2026-10-1{i}", "很長的一行 " + "疏水閥" * 80)
    from warroom.agent.audit import JsonlAuditLog
    audit = JsonlAuditLog(tmp_path / "a.jsonl")
    for k in range(5):
        audit.append(AuditKind.ANOMALY, "detector", {"message": "7 號 掉溫 " + "很長" * 60}, event_id=f"EV-2026100{k}-0800-CURE-07")
        audit.append(AuditKind.STATE_TRANSITION, "state_machine", {"to": "closed"}, event_id=f"EV-2026100{k}-0800-CURE-07")
    b = MM.for_machine("CURE-07", notes, audit, [], files=files)
    assert len(json.dumps(b, ensure_ascii=False)) <= MM.MAX_BLOCK_CHARS
    assert b["notes"] and len(b["past"]) == 3
