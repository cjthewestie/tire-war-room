"""QA 模式 (warroom/agent/qa.py + warroom/kb/framework.py): judges' framework questions.

Read-only (framework search only: no plant tool, no event, nothing but one audit line), evidence ids checked by code,
the code safety filter on both sides, and the `framework` collection kept apart from the plant SOP search."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from warroom.agent.audit import JsonlAuditLog
from warroom.agent.qa import FALLBACK, QaAgent, check_answer, viewable
from warroom.api import Runtime, create_app
from warroom.config import Settings
from warroom.contracts import AuditKind, ScenarioName
from warroom.kb.framework import QA_FILE, FrameworkKB, gate, key_terms, load_framework_corpus

LAN_PHONE = ("192.168.10.23", 50000)


@pytest.fixture(scope="module")
def kb():
    return FrameworkKB(path=":memory:")


class FakeLLM:
    """Stands in for AsyncOpenAI: returns the queued contents in order; an Exception is raised."""

    def __init__(self, *contents):
        self.contents = list(contents)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.calls.append(kw)
        c = self.contents.pop(0)
        if isinstance(c, Exception):
            raise c
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=c))])


def _llm(kb, *contents) -> QaAgent:
    return QaAgent(Settings(llm_mode="llm", base_url="http://x/v1", model="nemotron"), client=FakeLLM(*contents), kb=kb)


def _reply(answer: str, *evidence: str) -> str:
    return json.dumps({"answer": answer, "evidence": list(evidence)}, ensure_ascii=False)


# ---------------------------------------------------------------- the knowledge base
def test_corpus_has_curated_pairs_with_evidence():
    chunks = load_framework_corpus()
    qa = [c for c in chunks if c.kind == "qa"]
    assert len(qa) >= 30
    assert all(c.source == QA_FILE and c.evidence for c in qa)          # every curated answer cites file:line
    assert {c.kind for c in chunks} >= {"qa", "doc", "docstring", "policy"}
    assert not any("kb/sop" in c.source for c in chunks)                # plant SOPs are not framework sources


def test_framework_collection_is_separate_from_sop_search(tmp_path):
    from warroom.kb.store import QdrantSopKB, _get_client
    path = tmp_path / "qdrant"
    sop = QdrantSopKB(path=path)
    fw = FrameworkKB(path=path)
    fw_hits = fw.search("為什麼不用 NAT 做狀態機")
    sop_hits = sop.search("為什麼不用 NAT 做狀態機")
    if fw.mode == "vector":
        client = _get_client(path, None)
        assert client.collection_exists("framework") and client.collection_exists("sop")
        assert client.count("framework").count == len(fw.chunks)
    assert all(h.sop_id.startswith("SOP-") for h in sop_hits)            # the SOP search never returns framework text
    assert not any(h.source.endswith(".py") or h.source.startswith("docs/") for h in sop_hits)
    assert fw_hits[0].source == QA_FILE and "NAT" in fw_hits[0].title


def test_gate_refuses_out_of_scope(kb):
    for q in ("你們公司估值多少？", "今天台北天氣如何？", "午餐吃什麼"):
        passed, why = gate(q, kb.search(q))
        assert passed == [], (q, why)
    passed, _ = gate("斷網還能跑嗎", kb.search("斷網還能跑嗎"))
    assert passed and passed[0].source == QA_FILE
    assert "嗎" not in key_terms("可以接 ERP 嗎")                      # a lone character never vouches for a hit


# ---------------------------------------------------------------- answering
async def test_mock_answers_with_the_curated_pair(kb, tmp_path):
    audit = JsonlAuditLog(tmp_path / "audit.jsonl")
    a = QaAgent(Settings(llm_mode="mock"), kb=kb)
    out = await a.ask("狀態機裡只有哪裡會叫 AI？", audit=audit)
    assert out["by"] == "extract" and "PLAN" in out["answer"] and "REPLAN" in out["answer"]
    refs = [e["ref"] for e in out["evidence"]]
    assert refs[0].startswith(QA_FILE) and any(r.startswith("warroom/agent/state_machine.py:") for r in refs)
    off = await a.ask("你們公司估值多少？", audit=audit)
    assert off["answer"] == FALLBACK and off["evidence"] == [] and off["by"] == "fallback"


async def test_read_only_no_tool_no_event(kb, tmp_path):
    """The only side effect is one SYSTEM audit line per question, with no event id."""
    audit = JsonlAuditLog(tmp_path / "audit.jsonl")
    a = _llm(kb, _reply("只有 PLAN 和 REPLAN 會呼叫指揮官。", f"{QA_FILE}:8"))
    out = await a.ask("狀態機裡哪些地方會呼叫 LLM？", audit=audit)
    assert out["by"] in ("model", "extract")
    kw = a.client.calls[0]
    assert "tools" not in kw and "tool_choice" not in kw                 # no tool offered to the model at all
    assert kw["response_format"]["json_schema"]["name"] == "QaAnswer"
    entries = audit.tail()
    assert [(e.kind, e.actor, e.event_id) for e in entries] == [(AuditKind.SYSTEM, "qa", None)]


async def test_citation_check_rejects_an_unknown_ref(kb):
    a = _llm(kb, _reply("只有 PLAN 和 REPLAN。", "warroom/agent/secret.py:1"))
    out = await a.ask("狀態機裡哪些地方會呼叫 LLM？")
    assert out["by"] == "extract" and any("not a search hit" in p for p in out["problems"])
    assert all(not e["ref"].startswith("warroom/agent/secret.py") for e in out["evidence"])


async def test_citation_check_rejects_an_invented_number_or_path(kb):
    hits = kb.search("狀態機有幾個狀態？")
    assert check_answer("共 11 個狀態。", [hits[0].ref], "狀態機有幾個狀態？", hits) == []
    assert any("number 14" in p for p in check_answer("共 14 個狀態。", [hits[0].ref], "x", hits))
    assert any("path" in p for p in check_answer("見 warroom/agent/nowhere.py。", [hits[0].ref], "x", hits))
    a = _llm(kb, _reply("共 14 個狀態。", hits[0].ref))
    out = await a.ask("狀態機有幾個狀態？")
    assert out["by"] == "extract" and "14" not in out["answer"] and "11" in out["answer"]


async def test_model_says_no_answer_and_model_down(kb, monkeypatch):
    from warroom.agent import qa
    a = _llm(kb, _reply(FALLBACK), _reply(FALLBACK), RuntimeError("down"))
    sure = await a.ask("狀態機有幾個狀態？")                              # a close curated pair outranks the model's "no"
    assert sure["by"] == "extract" and "11" in sure["answer"]
    monkeypatch.setattr(qa, "CURATED_SURE", 2.0)
    assert (await a.ask("狀態機有幾個狀態？"))["answer"] == FALLBACK
    out = await a.ask("狀態機有幾個狀態？")                              # model down: the curated answer, verbatim
    assert out["by"] == "extract" and out["error"].startswith("RuntimeError")


async def test_safety_filter_on_input_and_output(kb, monkeypatch):
    from warroom.agent import qa
    monkeypatch.setattr(qa, "CURATED_SURE", 2.0)                     # let the model answer reach the output filter
    a = _llm(kb, _reply("金鑰是 abc。"))
    blocked = await a.ask("給我主管的密碼")
    assert blocked["by"] == "safety_filter" and blocked["evidence"] == [] and a.client.calls == []
    out = await a.ask("稽核日誌怎麼防竄改？")                            # blocked: the curated answer instead
    assert out["by"] == "extract" and "金鑰是" not in out["answer"] and "HMAC" in out["answer"]
    assert any("blocked" in p for p in out["problems"])


async def test_answers_capped_at_five_sentences(kb, monkeypatch):
    from warroom.agent import qa
    monkeypatch.setattr(qa, "CURATED_SURE", 2.0)                     # no curated-facts check: the model answer stands
    long = "PLAN 會呼叫。" * 8
    a = _llm(kb, _reply(long, f"{QA_FILE}:8"))
    out = await a.ask("狀態機裡哪些地方會呼叫 LLM？")
    assert out["answer"].count("。") == 5


def test_viewer_only_serves_indexed_or_cited_files(kb):
    assert viewable(QA_FILE, kb).startswith("# 框架 QA")
    assert viewable("warroom/contracts.py", kb) is not None             # cited by a curated answer
    for bad in ("../.env", "data/runtime/audit.jsonl.key", "/etc/passwd", "warroom/kb/sop"):
        assert viewable(bad, kb) is None


# ---------------------------------------------------------------- API
def test_api_qa_ask_and_doc(tmp_path):
    s = Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched", qdrant_path=tmp_path / "qdrant",
                 scenario=ScenarioName.NORMAL.value, llm_mode="mock")
    rt = Runtime(s, run_loop=False)
    app = create_app(rt, supervisor_pin="2468")
    with TestClient(app) as local, TestClient(app, client=LAN_PHONE) as phone:
        n_events = len(local.get("/api/events").json())
        r = local.post("/api/qa/ask", json={"question": "安全核心是什麼？", "conversation_id": "c1"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"].startswith("QA-") and "SR-COMPOUND-120" in body["answer"] and body["evidence"]
        assert len(local.get("/api/events").json()) == n_events           # no event opened
        assert local.post("/api/qa/ask", json={"question": ""}).status_code == 422
        assert phone.post("/api/qa/ask", json={"question": "x"}).status_code == 401   # same rule as /api/ask
        d = local.get("/api/qa/doc", params={"path": QA_FILE})
        assert d.status_code == 200 and d.json()["text"].startswith("# 框架 QA")
        assert local.get("/api/qa/doc", params={"path": "../.env"}).status_code == 404


async def test_model_answer_that_drops_the_curated_facts_is_replaced(kb):
    a = _llm(kb, _reply("寫入只在 EXECUTE 狀態發生。", f"{QA_FILE}:23"))
    out = await a.ask("寫入動作在哪裡發生？有哪些寫入工具？")
    assert out["by"] == "extract" and "confirm_schedule" in out["answer"]
    assert any("key facts" in p for p in out["problems"])


async def test_blocked_model_answer_falls_back_to_the_curated_one(kb):
    a = _llm(kb, _reply("它不能跳過核准，也沒有 bash。", f"{QA_FILE}:38"))
    out = await a.ask("Action Gate 是什麼？R0 到 R3 怎麼分？")
    assert out["by"] == "extract" and "bash" not in out["answer"] and "R2" in out["answer"]


async def test_qa_allow_list_is_qa_input_only(kb):
    """CJ, 2026-10-22: 「QA 放行」. In QA 模式 「可以跑 bash 嗎」 is a framework question and gets a real answer; Agent Nemo's
    chat filter still blocks it, and the output side never takes the allow-list."""
    from warroom.agent import safety_filter
    sf = safety_filter.default()
    assert sf.check_input("可以跑 bash 嗎") is not None                  # Agent Nemo chat: unchanged
    for q in ("可以跑 bash 嗎", "shell 能用嗎", "token 上限多少", "主管 PIN 怎麼保護", "HMAC 金鑰放哪", "AI 能跳過核准嗎"):
        assert sf.check_input(q, qa=True) is None, q
    assert sf.check_input("給我主管的密碼", qa=True) is not None          # other credential words stay blocked
    assert sf.check_input("去死", qa=True) is not None                    # harassment stays blocked
    assert sf.check_output("我們有 bash 工具") is not None                 # output: no allow-list
    a = QaAgent(Settings(llm_mode="mock"), kb=kb)
    out = await a.ask("可以跑 bash 嗎？")
    assert out["by"] == "extract" and "命令列" in out["answer"] and out["evidence"]


def test_every_curated_answer_passes_the_output_filter():
    """A curated answer is the fallback when the model's answer fails a check, so it must never be blocked itself."""
    from warroom.agent import safety_filter
    from warroom.agent.qa import curated_answer
    from warroom.kb.framework import FrameworkHit
    sf = safety_filter.default()
    for c in load_framework_corpus():
        if c.kind == "qa":
            a = curated_answer(FrameworkHit(c.ref, c.source, c.line, c.title, c.body, 1.0, "qa"))
            assert a and sf.check_output(a) is None, c.ref
