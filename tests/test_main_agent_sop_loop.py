"""Real run 10-21 (Lightning on the Spark): 「硫化機溫度掉下來要怎麼處理？」 → the model searched the SOP six times with
reworded queries and wrote no reply; the code-built reply then named the same SOP five times."""
import json
from types import SimpleNamespace

from warroom.agent.main_agent import MAX_SOP_SEARCHES, MainAgent
from warroom.contracts import ToolName, ToolResult


def _settings():
    return SimpleNamespace(llm_mode="llm", model="nemotron-test", base_url="http://x/v1", llm_timeout_s=10.0,
                           api_key_effective="none")


class _SopLooper:
    """Calls sop_search with a new wording every step and never writes a reply."""

    def __init__(self):
        self.n = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.n += 1
        if "tools" not in kw:                      # the last step offers no tools: still no words
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=None))])
        call = SimpleNamespace(id=f"c{self.n}", function=SimpleNamespace(
            name="sop_search", arguments=json.dumps({"query": f"硫化機溫度下降處置 第{self.n}次", "k": 10}, ensure_ascii=False)))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[call]))])


async def test_reworded_sop_searches_stop_and_the_reply_names_the_sop_once():
    ran = []

    async def run_tool(name, args, call_id):
        ran.append(name)
        data = {"hits": [{"sop_id": "SOP-CP-02", "title": "硫化機溫度下降處置", "text": "確認蒸汽壓"},
                         {"sop_id": "SOP-CP-01", "title": "硫化中斷處置", "text": "停止進模"}]}
        return ToolResult(call_id=call_id, tool=ToolName(name), ok=True, data=data)

    async def propose(task):
        raise AssertionError("a question is not a task")

    out = await MainAgent(_settings(), client=_SopLooper()).turn(
        "硫化機溫度掉下來要怎麼處理？", context={"abnormal": []}, waiting_plan=None,
        run_tool=run_tool, propose=propose, discuss=None)
    assert ran == ["sop_search"] * MAX_SOP_SEARCHES
    assert out["reply"].count("SOP-CP-02") == 1
