"""RAG no-answer gate (CJ, 2026-10-22: 「修 RAG」): a question the SOP knowledge base does not cover gets no hit and
「知識庫沒有相關 SOP」 instead of the nearest unrelated SOP; house and team SOPs are searched together; a reply citing
an SOP the search did not return is replaced. The eval set is tests/data/rag_eval.yaml (scripts/rag_eval.py)."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from warroom.agent.ask import summarize
from warroom.agent.main_agent import NO_SOP_REPLY, MainAgent, uncited_sops
from warroom.agent.state_machine import StateMachine
from warroom.contracts import ToolName, ToolResult
from warroom.kb.store import NO_SOP, QdrantSopKB, key_terms, subject
from warroom.kb.tools import sop_search_tool

EVAL = Path(__file__).parent / "data" / "rag_eval.yaml"


@pytest.fixture(scope="module")
def kb() -> QdrantSopKB:
    k = QdrantSopKB(path=":memory:", collection="sop_gate_test")
    k.ingest()
    return k


def _search(kb, q, k=3):
    return sop_search_tool(SimpleNamespace(kb=kb), {"query": q, "k": k})


def test_eval_set_passes_90_percent(kb):
    data = yaml.safe_load(EVAL.read_text(encoding="utf-8"))
    right = sum(bool(h := _search(kb, i["q"])["hits"]) and h[0]["sop_id"] in i["expect"] for i in data["in_kb"])
    empty = sum(not _search(kb, q)["hits"] for q in data["out_of_kb"])
    assert len(data["in_kb"]) + len(data["out_of_kb"]) >= 30
    assert (right + empty) / (len(data["in_kb"]) + len(data["out_of_kb"])) >= 0.9, (right, empty)


@pytest.mark.parametrize("q", ["叉車壞了", "怎麼做蛋糕", "停車場在哪", "印表機壞了"])
def test_gate_rejects_out_of_kb(kb, q):
    out = _search(kb, q)
    assert out["hits"] == [] and out["team_hits"] == []
    assert out["reason"].startswith(NO_SOP)
    assert summarize(ToolName.SOP_SEARCH, out, {"query": q}) == NO_SOP


@pytest.mark.parametrize(("q", "want"), [("缺料怎麼辦", "SOP-MT-01"), ("硫化機溫度掉下來怎麼處理", "SOP-CP-02"),
                                         ("蒸汽壓不足怎麼處理", "SOP-ST-01"), ("硫化中斷處置", "SOP-CP-01")])
def test_in_kb_still_hits(kb, q, want):
    out = _search(kb, q)
    assert out["hits"][0]["sop_id"] == want and out["reason"] == ""


def test_state_machine_queries_still_hit(kb):
    """The fixed queries the event flows send in CLASSIFY / PLAN / REPLAN must not be gated out."""
    for q in [*StateMachine.PLANT_SOP_QUERY.values(), "硫化機 溫度下降 蒸汽壓不足 延長硫化", "硫化中斷處置",
              "開煉機 膠溫過高 冷卻水流偏低", "膠料過熱 暫停投料 處置"]:
        assert _search(kb, q)["hits"], q


def test_subject_and_synonyms():
    assert subject("缺料怎麼辦") == "缺料"
    assert {"缺料", "斷料", "原料不足", "待補料"} <= key_terms("缺料怎麼辦")
    assert {"溫度偏低", "溫度下降", "掉溫"} <= key_terms("硫化機掉溫要怎麼處理")
    assert "壞了" not in key_terms("叉車壞了") and "叉車" in key_terms("叉車壞了")


def test_synonym_finds_the_sop_without_its_words(kb):
    """「斷料」 is not in the question's SOP title; the synonym group carries it to SOP-MT-01."""
    assert _search(kb, "斷料了要怎麼辦")["hits"][0]["sop_id"] == "SOP-MT-01"


def test_team_sops_merged_with_source_label(kb):
    out = _search(kb, "胎側膠怎麼做")
    assert out["hits"][0]["sop_id"] == "SOP-08" and out["hits"][0]["source_label"] == "團隊資料集"
    assert out["team_hits"][0]["sop_id"] == "SOP-08"
    mixed = _search(kb, "硫化機溫度掉下來怎麼處理")["hits"]
    assert {h["source_label"] for h in mixed} == {"工廠 SOP", "團隊資料集"}
    assert [h["score"] for h in mixed] == sorted((h["score"] for h in mixed), reverse=True)
    assert "（團隊資料集）" in summarize(ToolName.SOP_SEARCH, _search(kb, "胎側膠怎麼做"), {})


def test_uncited_sops():
    steps = [{"tool": "sop_search", "ok": True, "summary": "SOP：SOP-CP-02〈硫化機溫度下降處置〉…；SOP-05〈硫化機溫度偏低〉…"}]
    assert uncited_sops("請照 SOP-CP-02 與 SOP-05 處理。", steps) == []
    assert uncited_sops("請照 SOP-ST-01 處理。", steps) == ["SOP-ST-01"]
    assert uncited_sops("照 SOP-CP-02 處理", [{"tool": "sop_search", "ok": True, "summary": NO_SOP}]) == ["SOP-CP-02"]
    # no search this turn: an id that came with the context / last turn is allowed, a new one is not
    assert uncited_sops("照 SOP-CP-02 第二步", [], given="you_said: 請照 SOP-CP-02 處理") == []
    assert uncited_sops("照 SOP-XX-99", [], given="") == ["SOP-XX-99"]


class _Fabricator:
    """Searches the SOP once (no hit), then answers citing an SOP the search never returned."""

    def __init__(self):
        self.n = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.n += 1
        if self.n == 1:
            call = SimpleNamespace(id="c1", function=SimpleNamespace(
                name="sop_search", arguments=json.dumps({"query": "叉車壞了"}, ensure_ascii=False)))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[call]))])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="請依 SOP-SN-01 感測器漂移判定處理叉車，先檢查電瓶。", tool_calls=None))])


async def test_reply_citing_a_fabricated_sop_is_replaced():
    async def run_tool(name, args, call_id):
        return ToolResult(call_id=call_id, tool=ToolName(name), ok=True,
                          data={"query": args.get("query"), "hits": [], "team_hits": [], "reason": NO_SOP})

    async def propose(task):
        raise AssertionError("a question is not a task")

    settings = SimpleNamespace(llm_mode="llm", model="nemotron-test", base_url="http://x/v1", llm_timeout_s=10.0,
                               api_key_effective="none")
    out = await MainAgent(settings, client=_Fabricator()).turn(
        "叉車壞了怎麼辦", context={"abnormal": []}, waiting_plan=None, run_tool=run_tool, propose=propose, discuss=None)
    assert out["reply"] == NO_SOP_REPLY
    assert "SOP-SN-01" in out["error"]


class _Baker:
    """Never calls a tool: answers a how-to with general knowledge (a cake recipe)."""

    def __init__(self):
        self.seen: list[list[dict]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.seen.append(kw["messages"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="把蛋白打發，加入低筋麵粉，倒入模具烘烤 30 分鐘。", tool_calls=None))])


async def test_how_to_runs_sop_search_first_and_no_hit_is_said():
    ran = []

    async def run_tool(name, args, call_id):
        ran.append((name, args["query"]))
        return ToolResult(call_id=call_id, tool=ToolName(name), ok=True,
                          data={"query": args["query"], "hits": [], "team_hits": [], "reason": NO_SOP})

    async def propose(task):
        raise AssertionError("a question is not a task")

    settings = SimpleNamespace(llm_mode="llm", model="nemotron-test", base_url="http://x/v1", llm_timeout_s=10.0,
                               api_key_effective="none")
    baker = _Baker()
    out = await MainAgent(settings, client=baker).turn(
        "怎麼做蛋糕", context={"abnormal": []}, waiting_plan=None, run_tool=run_tool, propose=propose, discuss=None)
    assert ran == [("sop_search", "怎麼做蛋糕")]
    assert any(m.get("role") == "tool" and NO_SOP in m["content"] for m in baker.seen[0])   # the model saw the result
    assert out["reply"] == NO_SOP_REPLY
