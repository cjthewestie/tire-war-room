"""「接記憶」 (CJ, 2026-10-22): the supervisor writes a note once in 後台 and the agents use it next time, and what happened
before on a machine comes back when that machine has a problem again.

- the commander's per-event context carries context.machine_memory for the event's machine only (notes ≤5 newest
  first, ≤120 chars, past events ≤3), the system prompt stays byte-identical, TRIM_STEPS cuts it second;
- every commander llm_call row records which note and past-event ids were attached, the timeline says so;
- Agent Nemo reads it through query_plant machine_history (read-only) and gets it with a read about one machine."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from tests.test_agent import _req_plan
from tests.test_agent_page11 import page11_anomaly
from tests.test_context_budget import LONG, _settings, _sop
from tests.test_main_agent_demo import Floor, Model
from tests.test_main_agent_demo import _settings as _chat_settings
from tests.test_main_agent_query import Reader
from tests.test_plan_talk import Plant, _rt
from warroom import factory as F
from warroom.agent.audit import JsonlAuditLog
from warroom.agent.commander import SYSTEM_PROMPT, TRIM_STEPS, build_messages, build_user_message, fit_context
from warroom.agent.main_agent import QUERY, MainAgent, rule_reads, topic_for
from warroom.api import plant_query
from warroom.contracts import AuditKind, ScenarioName
from warroom.data import machine_memory as MM
from warroom.data.memory_notes import MemoryNotes

NOW = datetime(2026, 10, 3, 8, 0, tzinfo=F.TZ)


def _closed(audit, eid: str, message: str, cause: str = "steam_trap_stuck", verdict: str = "confirmed") -> None:
    audit.append(AuditKind.ANOMALY, "detector", {"message": message}, event_id=eid)
    audit.append(AuditKind.LLM_CALL, "commander", {"phase": "plan", "hypothesis": "H1", "cause_code": cause}, event_id=eid)
    audit.append(AuditKind.SYSTEM, "verifier", {"hypothesis": "H1", "verdict": verdict}, event_id=eid)
    audit.append(AuditKind.STATE_TRANSITION, "state_machine", {"from": "verify", "to": "closed"}, event_id=eid)


@pytest.fixture
def mem(tmp_path):
    notes = MemoryNotes(tmp_path / "memory_notes.json")
    audit = JsonlAuditLog(tmp_path / "audit.jsonl")
    for i in range(7):                                   # 7 notes on press 7: the 5 newest go
        notes.add("CURE-07", f"7 號機筆記 {i} " + "長" * (200 if i == 6 else 0), "admin:sup", NOW + timedelta(minutes=i))
    notes.add("CURE-08", "8 號機的筆記", "admin:sup", NOW)
    for k in range(4):
        _closed(audit, f"EV-20261003-08{k}000-CURE-07", f"7 號 第 {k} 次掉溫")
    _closed(audit, "EV-20261003-090000-CURE-08", "8 號 掉溫")
    return notes, audit


# ---------------------------------------------------------------- the block
def test_block_is_for_one_machine_only_and_compact(mem):
    notes, audit = mem
    b = MM.for_machine("CURE-07", notes, audit, [])
    assert b["machine_id"] == "CURE-07" and b["use"] == "機台筆記與過去事件只供參考假設，數字仍以工具結果為準"
    assert len(b["notes"]) == 5 and b["notes"][0]["text"].startswith("7 號機筆記 6")     # newest first
    assert all(len(n["text"]) <= MM.NOTE_CHARS and n["by"] == "admin:sup" and n["date"] == "2026-10-03" for n in b["notes"])
    assert len(b["past"]) == 3 and all(p["id"].endswith("CURE-07") for p in b["past"])
    assert b["past"][0]["symptom"].startswith("7 號 第") and b["past"][0]["cause"] == "H1 steam_trap_stuck（確認）"
    assert "8 號" not in json.dumps(b, ensure_ascii=False)
    assert MM.for_machine("CURE-01", notes, audit, []) is None                           # nothing to remember: no block
    assert MM.for_machine("NOPE", notes, audit, []) is None


def test_dataset_incidents_are_past_events(tmp_path):
    b = MM.for_machine("CURE-05", MemoryNotes(tmp_path / "n.json"), JsonlAuditLog(tmp_path / "a.jsonl"), [])
    assert [p["id"] for p in b["past"]] == ["INC-007"] and b["past"][0]["source"] == "team_dataset"
    assert b["past"][0]["symptom"] == "疏水閥卡滯" and b["past"][0]["action"] == "更換疏水閥"


# ---------------------------------------------------------------- commander request
def test_system_prompt_is_byte_identical_with_memory(mem):
    notes, audit = mem
    bare = _req_plan(page11_anomaly(), context={"sop": _sop("確認蒸汽壓")})
    withm = _req_plan(page11_anomaly(), context={"sop": _sop("確認蒸汽壓"),
                                                 "machine_memory": MM.for_machine("CURE-07", notes, audit, [])})
    assert build_messages(bare)[0]["content"] == build_messages(withm)[0]["content"]
    assert "machine_memory" not in SYSTEM_PROMPT and "7 號機筆記" not in build_messages(withm)[0]["content"]
    user = json.loads(build_user_message(withm))
    assert user["context"]["machine_memory"]["use"] == MM.USE                         # the one line on how to use it


def test_machine_memory_is_trimmed_second(mem):
    notes, audit = mem
    assert TRIM_STEPS[:3] == ("context.sop.text", "context.machine_memory", "context.neighbours")
    nb = {"CURE-08": {"temp_c_now": 127.9, "temp_c_min": 127.5}}
    block = MM.for_machine("CURE-07", notes, audit, [])
    big = {**block, "notes": [{**n, "text": LONG[:4000]} for n in block["notes"]]}   # only cutting it makes it fit
    req = _req_plan(page11_anomaly(), context={"sop": _sop(LONG), "machine_memory": big, "neighbours": nb})
    sent, _, trimmed = fit_context(req, SYSTEM_PROMPT, _settings(), 4096)
    assert trimmed == ["context.sop.text", "context.machine_memory"]
    assert "machine_memory" not in sent.context and sent.context["neighbours"] == nb
    small = _req_plan(page11_anomaly(), context={"sop": _sop(LONG), "machine_memory": block, "neighbours": nb})
    sent, _, trimmed = fit_context(small, SYSTEM_PROMPT, _settings(), 4096)
    assert trimmed == ["context.sop.text"] and sent.context["machine_memory"] == block   # small: it stays


@pytest.mark.timeout(300)
async def test_commander_gets_the_events_machine_memory_and_the_audit_says_which(tmp_path):
    seen = []
    async with Plant(_rt(tmp_path, ScenarioName.PRESS7_TEMP_DROP.value)) as p:
        rt = p.rt
        n7 = rt.memory_notes.add("CURE-07", "7 號機蒸汽閥上週換過，疏水閥易卡", "admin:sup", rt.clock.now())
        rt.memory_notes.add("CURE-08", "8 號機的筆記", "admin:sup", rt.clock.now())
        cmd = rt.c.state_machine.commander
        orig = cmd.plan

        async def spy(req, *a, **kw):
            seen.append(req)
            return await orig(req, *a, **kw)
        cmd.plan = spy
        await p.until(lambda: seen and any(e.machine_id == "CURE-07" and e.plan for e in rt.events()))
        ev = next(e for e in rt.events() if e.machine_id == "CURE-07")
        req = next(r for r in seen if r.event_id == ev.id)
        m = req.context["machine_memory"]
        assert m["machine_id"] == "CURE-07" and [x["id"] for x in m["notes"]] == [n7.id]
        assert "8 號機的筆記" not in json.dumps(req.context, ensure_ascii=False)
        rows = [e for e in rt.audit.tail(0, 100_000) if e.kind == AuditKind.LLM_CALL and e.actor == "commander"
                and e.event_id == ev.id]
        assert rows and rows[0].payload["machine_memory"] == {"note_ids": [n7.id], "past_event_ids": [], "trimmed": False}
        assert any(t.text == "參考了 7 號機筆記 1 則" and t.actor == "系統" for t in ev.timeline)


# ---------------------------------------------------------------- Agent Nemo
@pytest.mark.timeout(300)
async def test_machine_history_and_notes_topics_are_read_only(tmp_path):
    async with Plant(_rt(tmp_path, "normal")) as p:
        rt = p.rt
        rt.memory_notes.add("CURE-07", "7 號機蒸汽閥上週換過，疏水閥易卡", "admin:sup", rt.clock.now())
        before, version = len(rt.audit.tail(0, 1_000_000)), rt.c.store.current().version
        h = plant_query.query(rt, "machine_history", machine_id="CURE-07")
        assert h["notes"][0]["text"] == "7 號機蒸汽閥上週換過，疏水閥易卡" and h == rt.machine_memory("CURE-07")
        assert "error" in plant_query.query(rt, "machine_history")
        n = plant_query.query(rt, "memory_notes")
        assert n["count"] == 1 and n["notes"][0]["machine"] == "7 號機"
        assert len(rt.audit.tail(0, 1_000_000)) == before and rt.c.store.current().version == version
        assert len(rt.memory_notes.all()) == 1
        r = await p.http.post("/api/main/message", json={"text": "我在後台寫了什麼"})
        assert r.status_code == 200 and [s["args"] for s in r.json()["steps"]] == [{"topic": "memory_notes"}]
        res = [e for e in rt.audit.tail(0, 1_000_000) if e.actor == "tool:query_plant" and e.kind == AuditKind.TOOL_RESULT]
        assert res[-1].payload["data"]["topic"] == "memory_notes"
        r = await p.http.post("/api/main/message", json={"text": "7 號機以前出過什麼事"})
        rows = [e for e in rt.audit.tail(0, 1_000_000) if e.actor == "tool:query_plant"]
        assert rows[-2].payload["args"] == {"topic": "machine_history", "machine_id": "CURE-07"}
        assert rows[-1].payload["data"]["note_ids"] and rows[-1].payload["data"]["past_event_ids"] == []


def test_topics_for_memory_questions():
    assert topic_for("我在後台寫了什麼") == "memory_notes"
    assert topic_for("7 號機以前出過什麼事") == "machine_history"
    assert topic_for("之前有哪些訂單遲交") == "orders_history"          # no machine: not a machine's history
    assert rule_reads("7 號機以前出過什麼事", {}) == [(QUERY, {"topic": "machine_history", "machine_id": "CURE-07"})]
    assert rule_reads("我在後台寫了什麼", {}) == [(QUERY, {"topic": "memory_notes"})]


async def test_a_read_about_one_machine_brings_its_memory_once():
    hist = {"machine_id": "CURE-07", "use": MM.USE, "notes": [{"id": "MN-1", "text": "疏水閥易卡"}], "past": []}
    reader = Reader(hist)
    m = Model(("tool", "read_sensors", {"machine_ids": ["CURE-07"]}), ("tool", "read_sensors", {"machine_ids": ["CURE-07"], "minutes": 5}),
              ("say", "7 號機現在 122.3°C；後台筆記說疏水閥易卡。"))
    out = await MainAgent(_chat_settings(), client=m).turn("7 號機現在怎樣？", context={"abnormal": []}, waiting_plan=None,
                                                           run_tool=Floor().run_tool, propose=Floor().propose,
                                                           discuss=None, read_plant=reader)
    assert reader.topics == ["machine_history"] and reader.kw == {"machine_id": "CURE-07"}      # once, not per read
    tool_msgs = [x["content"] for x in m.calls[1]["messages"] if x["role"] == "tool"]
    assert "疏水閥易卡" in tool_msgs[0] and "只供參考" in tool_msgs[0]
    assert [s["tool"] for s in out["steps"]] == ["read_sensors", QUERY, "read_sensors"]


async def test_the_model_asking_for_history_it_already_got_reads_nothing_again():
    reader = Reader({"machine_id": "CURE-07", "use": MM.USE, "notes": [{"id": "MN-1", "text": "疏水閥易卡"}], "past": []})
    m = Model(("tool", "read_sensors", {"machine_ids": ["CURE-07"]}),
              ("tool", QUERY, {"topic": "machine_history", "machine_id": "7"}), ("say", "7 號機 122.3°C。"))
    await MainAgent(_chat_settings(), client=m).turn("7 號機現在怎樣？", context={"abnormal": []}, waiting_plan=None,
                                                     run_tool=Floor().run_tool, propose=Floor().propose, discuss=None,
                                                     read_plant=reader)
    assert reader.topics == ["machine_history"]
    assert any("剛查過了" in x["content"] for x in m.calls[2]["messages"] if x["role"] == "tool")


async def test_a_general_or_two_machine_chat_does_not_bring_memory():
    reader = Reader({"notes": []})
    m = Model(("tool", "read_sensors", {"machine_ids": ["CURE-07", "CURE-08"]}), ("say", "兩台都在跑。"))
    await MainAgent(_chat_settings(), client=m).turn("7 號機和 8 號機現在怎樣？", context={"abnormal": []}, waiting_plan=None,
                                                     run_tool=Floor().run_tool, propose=Floor().propose, discuss=None,
                                                     read_plant=reader)
    assert reader.topics == []
