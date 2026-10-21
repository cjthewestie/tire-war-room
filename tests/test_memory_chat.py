"""claude-mem style memory in Agent Nemo's overview chat only (CJ, 2026-10-22: 「可以試著套用claude mem的方法嗎然後只有在
Agent Nemo 對話統整區有這個功能(非單一任務區)就是今天有哪些訂單今天做過哪些任務之類的」).

Observations are captured by code from the audit (no model), the overview chat gets today's as a capped index and two
reads (memory_search, memory_get), a task-bound chat gets none of it, and the reply's numbers stay grounded."""
from __future__ import annotations

import json

import pytest

from scripts.rebuild_memory import rebuild
from tests.test_main_agent_demo import Floor, Model
from tests.test_main_agent_demo import _settings as _chat_settings
from tests.test_plan_talk import Plant, _rt
from warroom.agent.main_agent import MEM_GET, MEM_SEARCH, MainAgent
from warroom.contracts import ScenarioName
from warroom.data import memory_log as ML

TS = "2026-10-21T09:{:02d}:00+08:00"


def _row(seq, kind, actor, payload, eid=None):
    return {"seq": seq, "ts": TS.format(seq % 60), "kind": kind, "actor": actor, "event_id": eid, "payload": payload}


def test_capture_makes_the_right_typed_observations():
    eid = "EV-20261021-090000-CURE-07"
    rows = [
        _row(1, "anomaly", "detector", {"machine_id": "CURE-07", "message": "7 號 128 → 122°C"}, eid),
        _row(2, "approval_request", "state_machine", {"version": 2, "reason": "假設 H1：蒸汽壓不足", "actions": ["#A-311 改 6 號"]}, eid),
        _row(3, "approval_decision", "supervisor", {"decision": "approve", "option_id": "B"}, eid),
        _row(4, "approval_decision", "supervisor", {"decision": "reject", "reason": "不要調人"}, eid),
        _row(5, "system", "verifier", {"hypothesis": "H1", "verdict": "rejected"}, eid),
        _row(6, "state_transition", "state_machine", {"to": "safe_stop"}, eid),
        _row(7, "safety_action", "safety", {"machine_id": "CURE-07", "message": "7 號機 119.9°C < 120°C：扣模"}, eid),
        _row(8, "task", "world", {"status": "完成", "title": "檢查蒸汽閥", "task_id": "TK-1"}, eid),
        _row(9, "system", "world", {"kind": "order_rush", "msg": "急單 #R-101 進來", "order_ids": ["#R-101"]}),
        _row(10, "system", "world", {"kind": "shipment_departed", "msg": "出貨 #A-311", "order_ids": ["#A-311"], "on_time": True}),
        _row(11, "system", "agent_nemo", {"msg": "pre-plan", "order_id": "#M-201"}),
        _row(12, "system", "admin:sup", {"admin": "memory_note_add", "machine_id": "CURE-07", "text": "疏水閥易卡"}),
        _row(13, "system", "main_agent", {"orch": "chat", "text": "不要動 9 號機", "pinned": "不要動 9 號機"}),
        _row(14, "tool_call", "tool:read_sensors", {}, eid),                       # not an observation
        _row(15, "system", "main_agent", {"orch": "chat", "text": "你好"}),         # neither
    ]
    obs = [ML.observe(r) for r in rows]
    assert [o["type"] if o else None for o in obs] == ["event", "event", "approve", "reject", "event", "event", "safety",
                                                        "task", "order", "ship", "order", "note", "note", None, None]
    assert obs[0]["id"] == "OB-0001" and obs[0]["machines"] == ["CURE-07"] and obs[0]["ref"] == eid
    assert obs[1]["title"] == "7 號機 計劃 v2 送核准" and obs[1]["orders"] == ["#A-311"]
    assert obs[4]["title"] == "7 號機H1 被推翻" and obs[5]["title"] == "7 號機安全停止"
    assert obs[8]["orders"] == ["#R-101"] and obs[10]["title"] == "預先規劃：#M-201 有交期風險"
    assert all(len(o["title"]) <= 40 for o in obs if o)
    assert ML.index_line(obs[0]).startswith("OB-0001 · 09:01 · 🛠 · ")


def test_the_index_is_capped():
    obs = [{"id": f"OB-{i:04d}", "seq": i, "ts": TS.format(i % 60), "type": ("order", "event", "task", "note")[i % 4],
            "title": f"第 {i} 筆紀錄，標題寫得長一點讓它超過字數" + "長" * 10} for i in range(1, 120)]
    idx = ML.day_index(obs)
    lines = [x for x in idx.splitlines() if x.startswith("OB-")]
    assert len(lines) <= ML.INDEX_MAX_LINES and len(idx) <= ML.INDEX_MAX_CHARS + 60
    assert "【訂單】" in idx and "用 memory_search 查" in idx
    assert ML.day_index(obs[:5]).splitlines()[0].startswith("OB-0005")          # newest first, no groups when short


@pytest.mark.timeout(300)
async def test_observations_live_match_the_rebuild(tmp_path):
    async with Plant(_rt(tmp_path, ScenarioName.PRESS7_TEMP_DROP.value)) as p:
        await p.until(lambda: p.pending())
        live = p.rt.memory_desk.today()
        assert any(o["type"] == "event" and "計劃 v1 送核准" in o["title"] for o in live)
    rebuild(tmp_path / "audit.jsonl", at="x")
    rebuilt = ML.ObservationStore(tmp_path / "observations.sqlite").all()
    assert [o["id"] for o in live] == [o["id"] for o in rebuilt if o["id"] in {x["id"] for x in live}]


@pytest.mark.timeout(300)
async def test_the_index_is_only_in_the_overview_chat(tmp_path, monkeypatch):
    seen = []

    async def spy(self, text, **kw):
        seen.append(kw)
        return {"id": "MAIN-1", "reply": "好", "steps": [], "event_id": None, "by": "model", "fell_back": False,
                "error": None, "intent": None}
    monkeypatch.setattr(MainAgent, "turn", spy)
    async with Plant(_rt(tmp_path, ScenarioName.PRESS7_TEMP_DROP.value)) as p:
        await p.until(lambda: p.pending())
        eid = p.pending()[0].event_id
        assert (await p.http.post("/api/main/message", json={"text": "今天做過哪些任務"})).status_code == 200
        assert (await p.http.post("/api/main/message", json={"text": "這份計劃為什麼", "event_id": eid})).status_code == 200
    overview, task = seen
    assert "OB-" in overview["context"]["today_index"] and overview["memory"] is not None
    assert "today_index" not in task["context"] and task["memory"] is None


class Mem:
    def __init__(self):
        self.calls = []

    def search(self, q, date, type_, call_id):
        self.calls.append(("search", q))
        return {"found": True, "hits": [{"id": "OB-0009", "date": "2026-10-21", "text": "急單 #R-101 進來"}]} if "急單" in q \
            else {"found": False, "hits": []}

    def get(self, ids, call_id):
        self.calls.append(("get", ids))
        return [{"id": "OB-0009", "facts": {"kind": "order_rush", "qty": 12}, "title": "急單 #R-101 進來"}] if "OB-0009" in ids else []


INDEX = "OB-0012 · 10:40 · 🚚 · 出貨 #R-101\nOB-0009 · 08:05 · 📦 · 急單 #R-101 進來"


async def _chat(model, text, memory, index=INDEX, floor=None):
    f = floor or Floor()
    ctx = {"abnormal": [], **({"today_index": index} if index is not None else {})}
    return await MainAgent(_chat_settings(), client=model).turn(text, context=ctx, waiting_plan=None, run_tool=f.run_tool,
                                                                propose=f.propose, discuss=None, memory=memory), f


async def test_history_questions_answer_from_the_index_without_a_forced_read():
    m = Model(("say", "早上 08:05 進來的急單 #R-101，10:40 已經出貨。"))
    out, f = await _chat(m, "早上那張急單後來呢", Mem())
    assert out["steps"] == [] and f.tools == [] and "#R-101" in out["reply"]
    names = [t["function"]["name"] for t in m.calls[0]["tools"]]
    assert MEM_SEARCH in names and MEM_GET in names
    user = json.loads(m.calls[0]["messages"][1]["content"])
    assert "today_index" in user["context"] and user["memory_rule"].startswith("context.today_index")


async def test_memory_get_returns_the_facts():
    mem = Mem()
    m = Model(("tool", MEM_GET, {"ids": ["ob-0009"]}), ("say", "急單 #R-101 共 12 條。"))
    out, _ = await _chat(m, "那張急單有幾條", mem)
    assert mem.calls == [("get", ["OB-0009"])] and out["steps"][0]["label"] == "查了記憶（細節）"
    tool = [x["content"] for x in m.calls[1]["messages"] if x["role"] == "tool"][0]
    assert '"qty": 12' in tool and out["reply"] == "急單 #R-101 共 12 條。"


async def test_memory_search_says_nothing_found_when_the_gate_closes():
    mem = Mem()
    m = Model(("tool", MEM_SEARCH, {"q": "斷料停線"}), ("say", "記憶裡沒有找到斷料停線的紀錄。"))
    out, _ = await _chat(m, "之前有沒有斷料停線過", mem)
    tool = [x["content"] for x in m.calls[1]["messages"] if x["role"] == "tool"][0]
    assert "找不到" in tool and "沒有找到" in out["reply"]


async def test_a_task_chat_has_no_memory_tools():
    m = Model(("say", "好。"))
    await _chat(m, "今天做過哪些任務", Mem(), index=None)
    assert MEM_SEARCH not in [t["function"]["name"] for t in m.calls[0].get("tools") or []]


async def test_numbers_not_in_the_index_do_not_reach_the_reply():
    m = Model(("say", "今天出貨 9 批，急單 #R-101 已出貨。"))
    out, _ = await _chat(m, "今天出貨幾批", Mem())
    assert "9 批" not in out["reply"]


def test_yesterday_questions_go_to_the_daily_summary():
    from warroom.agent.main_agent import QUERY, rule_reads, topic_for
    assert topic_for("昨天發生什麼事") == "daily_summary"
    assert rule_reads("昨天發生什麼事", {}) == [(QUERY, {"topic": "daily_summary"})]
