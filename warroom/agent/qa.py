"""QA 模式: answers finals judges' technical questions about the agent framework (CJ, 2026-10-22: 「QA mode是評審遇到
技術框架問題的時候可以幫我回答的」).

Read-only by construction: the only thing it reads is the framework knowledge base (warroom/kb/framework.py, its own
Qdrant collection, never the plant SOPs). It has no plant tool, no propose_plan, opens no event and writes nothing to
the plant; the audit log gets one SYSTEM line per question (actor "qa", no event id).

One question:
1. The code safety filter (config/safety/blocked_terms.yaml) on the input: a hit answers with its fixed line.
2. Framework search + the no-answer gate. Nothing passes → FALLBACK, no model call.
3. LLM mode: one model call (JSON {answer, evidence}) in a ModelQueue slot at the plant-question priority ("task"),
   so an incident plan still goes first. Mock mode: the best curated answer, verbatim.
4. Post-model check by code: every cited evidence id and every file path in the answer must be one of the hits, and
   every number must appear in the hit text or the question. A miss → the curated answer of the best hit, verbatim
   (by = "extract"), or FALLBACK when there is none. At most MAX_SENTENCES sentences.
5. The safety filter on the output.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict
from typing import Any

from warroom.agent import safety_filter
from warroom.contracts import AuditKind, LLMMode
from warroom.kb.framework import FrameworkHit, FrameworkKB, default_framework_kb, gate

FALLBACK = "這部分我們文件裡沒有，現場由 CJ 補充。"
MAX_SENTENCES = 5
SEARCH_K = 5
HISTORY_TURNS = 2           # earlier Q/A pairs of the same conversation the model sees (follow-ups: 「那它呢？」)
MAX_CONVERSATIONS = 64

QA_SYSTEM = """你是「實心胎智慧製造戰情室」團隊的工程師，正在決賽現場回答評審對 agent 框架的技術問題。
規則：
1. 只根據使用者訊息裡的「文件片段」回答；片段沒寫的不要補充、不要猜。
2. 繁體中文，口語，像當面回答評審；第一句講結論，再用 2 到 4 句講片段裡具體的機制、名稱和數字，總共最多 5 句。
3. 數字、狀態名、規則編號照片段原文寫，不要自己換算或補數字。
4. evidence 只填片段的 ref（例如 docs/qa/framework-qa.md:23），挑真的用到的 1 到 3 個。
5. 片段標「未驗證」的內容要直說「這點還沒驗證」。
6. 不要寫英文單字 bash、shell、PIN；說「命令列」「主管驗證碼」。
7. 片段答不了這題：answer 寫「這部分我們文件裡沒有，現場由 CJ 補充。」，evidence 留空。"""

_NUM_RX = re.compile(r"\d+(?:\.\d+)?")
_PATH_RX = re.compile(r"[\w./-]+\.(?:py|md|ya?ml|ts|tsx|json|toml)\b")
_SENT_RX = re.compile(r"[^。！？!?]+[。！？!?]?")


def qa_schema(refs: list[str]) -> dict[str, Any]:
    ev: dict[str, Any] = {"type": "string", "enum": refs} if refs else {"type": "string"}
    return {"type": "object", "additionalProperties": False, "required": ["answer", "evidence"],
            "properties": {"answer": {"type": "string", "maxLength": 500},
                           "evidence": {"type": "array", "maxItems": 3, "items": ev}}}


def curated_answer(hit: FrameworkHit) -> str | None:
    """The `A:` line of a curated QA pair (docs/qa/framework-qa.md), verbatim."""
    if hit.kind != "qa":
        return None
    m = re.search(r"^A:\s*(.+)$", hit.text, re.MULTILINE)
    return m.group(1).strip() if m else None


def cap_sentences(text: str, n: int = MAX_SENTENCES) -> str:
    parts = [p for p in _SENT_RX.findall(text.strip()) if p.strip()]
    return "".join(parts[:n]).strip() if len(parts) > n else text.strip()


def check_answer(answer: str, evidence: list[str], question: str, hits: list[FrameworkHit]) -> list[str]:
    """Code checks the model. Returns the problems ([] = the answer stands)."""
    refs = {h.ref for h in hits}
    files = {h.source for h in hits} | {e.rsplit(":", 1)[0] for h in hits for e in h.evidence}
    problems = [f"evidence {e} is not a search hit" for e in evidence if e not in refs]
    problems += [f"path {p} is not in the hits" for p in _PATH_RX.findall(answer)
                 if p not in files and not any(f.endswith(p) for f in files)]
    source = question + " " + " ".join(f"{h.title} {h.text} {' '.join(h.evidence)}" for h in hits)
    known = set(_NUM_RX.findall(source))
    known |= {n.split(".")[0] for n in known}
    problems += [f"number {n} is not in the hits" for n in _NUM_RX.findall(answer) if n not in known]
    return problems


def _live_page(path: str) -> dict[str, str] | None:
    """The live war-room page that shows what a file does (the 證據 list links to it)."""
    if path.endswith(("state_machine.py", "contracts.py")):
        return {"href": "#sm", "label": "狀態機"}
    if path.endswith(("audit.py", "gate.py", "main_agent.py", "commander.py")):
        return {"href": "#console", "label": "Agent 過程與稽核"}
    if path.endswith(("plant.py", "admin.py", "PLANT.md", "blocked_terms.yaml", "safety_filter.py")):
        return {"href": "#admin", "label": "後台"}
    return None


def evidence_list(cited: list[str], hits: list[FrameworkHit]) -> list[dict[str, Any]]:
    """The cited hits, then the code lines a cited curated pair points at (its 證據), deduplicated, in order."""
    by_ref = {h.ref: h for h in hits}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(ref: str, title: str, kind: str) -> None:
        if ref in seen:
            return
        seen.add(ref)
        path, _, line = ref.rpartition(":")
        out.append({"ref": ref, "path": path, "line": int(line) if line.isdigit() else None, "title": title,
                    "kind": kind, "live": _live_page(path)})

    for ref in cited:
        h = by_ref.get(ref)
        if h is None:
            continue
        add(h.ref, h.title, h.kind)
        for e in h.evidence:
            add(e, "程式／文件出處", "code")
    return out


class QaAgent:
    """`client` (an AsyncOpenAI) and `kb` are injectable for tests."""

    def __init__(self, settings: Any, client: Any = None, kb: FrameworkKB | None = None,
                 sf: safety_filter.SafetyFilter | None = None):
        self.settings = settings
        self.mode = LLMMode(settings.llm_mode)
        self.kb = kb
        self.sf = sf or safety_filter.default()
        self.client = client
        self.server_kind = "openai"
        self._history: OrderedDict[str, list[tuple[str, str]]] = OrderedDict()
        self._n = 0
        if self.mode is LLMMode.LLM and client is None:
            from openai import AsyncOpenAI

            from warroom.agent.commander import detect_server_kind
            self.client = AsyncOpenAI(base_url=settings.base_url, api_key=settings.api_key_effective,
                                      timeout=settings.llm_timeout_s, max_retries=0)
            self.server_kind = detect_server_kind(settings.base_url)

    def _kb(self) -> FrameworkKB:
        if self.kb is None:
            self.kb = default_framework_kb()
        return self.kb

    def _extra(self) -> dict[str, Any]:
        from warroom.agent.commander import thinking_off
        extra = thinking_off(self.server_kind)
        return {"extra_body": extra} if extra else {}

    def _remember(self, conv: str | None, q: str, a: str) -> None:
        if not conv:
            return
        h = self._history.setdefault(conv, [])
        h.append((q, a))
        del h[:-HISTORY_TURNS]
        self._history.move_to_end(conv)
        while len(self._history) > MAX_CONVERSATIONS:
            self._history.popitem(last=False)

    async def _llm(self, question: str, hits: list[FrameworkHit], history: list[tuple[str, str]]) -> tuple[str, list[str]]:
        frags = "\n\n".join(f"[ref: {h.ref}]（{h.title}）\n{h.text}" for h in hits)
        prev = "".join(f"評審先前問：{q}\n我們答：{a}\n" for q, a in history)
        user = f"{prev}評審問：{question}\n\n文件片段：\n{frags}"
        r = await self.client.chat.completions.create(
            model=self.settings.model, temperature=0.0, max_tokens=450,
            messages=[{"role": "system", "content": QA_SYSTEM}, {"role": "user", "content": user}],
            response_format={"type": "json_schema", "json_schema": {"name": "QaAnswer", "schema": qa_schema([h.ref for h in hits])}},
            **self._extra())
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (r.choices[0].message.content or "").strip())
        data = json.loads(text)
        return str(data.get("answer") or "").strip(), [str(e) for e in data.get("evidence") or []]

    async def ask(self, question: str, *, conversation_id: str | None = None, audit: Any = None,
                  slot: Any = None, secrets: list[str] | None = None) -> dict[str, Any]:
        """{id, question, answer, evidence, by, gate, problems, error, ms}. `slot()` returns the ModelQueue context
        manager for the model call (None: no queue)."""
        t0 = time.perf_counter()
        self._n += 1
        qa_id = f"QA-{self._n:04d}"
        q = question.strip()
        out: dict[str, Any] = {"id": qa_id, "question": q, "answer": FALLBACK, "evidence": [], "by": "fallback",
                               "gate": None, "problems": [], "error": None, "hits": [], "conversation_id": conversation_id}

        def done() -> dict[str, Any]:
            out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
            if audit is not None:
                audit.append(AuditKind.SYSTEM, "qa", {"msg": "qa", "id": qa_id, "question": q, "answer": out["answer"],
                                                      "by": out["by"], "gate": out["gate"], "problems": out["problems"],
                                                      "error": out["error"], "evidence": [e["ref"] for e in out["evidence"]],
                                                      "ms": out["ms"]})
            return out

        hit = self.sf.check_input(q)
        if hit is not None:
            out.update(answer=hit.reply, by="safety_filter", gate=f"blocked:{hit.category}")
            return done()

        history = self._history.get(conversation_id or "", [])
        kb = self._kb()
        hits = await asyncio.to_thread(kb.search, q, SEARCH_K)
        passed, why = gate(q, hits)
        if not passed and history and len(q) <= 12:          # a short follow-up: search with the previous question too
            hits = await asyncio.to_thread(kb.search, f"{history[-1][0]} {q}", SEARCH_K)
            passed, why = gate(f"{history[-1][0]} {q}", hits)
        out["gate"] = why
        out["hits"] = [{"ref": h.ref, "title": h.title, "score": h.score} for h in hits]
        if not passed:
            return done()

        best = passed[0]
        extract = curated_answer(best) or next((a for h in passed if (a := curated_answer(h))), None)
        answer, cited = extract or FALLBACK, [best.ref] if extract else []
        by = "extract" if extract else "fallback"
        if self.mode is LLMMode.LLM:
            try:
                async def call() -> tuple[str, list[str]]:
                    return await self._llm(q, passed, history)
                if slot is not None:
                    async with slot():
                        m_answer, m_cited = await call()
                else:
                    m_answer, m_cited = await call()
                if not m_answer or "文件裡沒有" in m_answer:
                    answer, cited, by = FALLBACK, [], "model_no_answer"
                else:
                    m_answer = cap_sentences(m_answer)
                    problems = check_answer(m_answer, m_cited, q, passed)
                    if problems:
                        out["problems"] = problems
                    else:
                        answer, cited, by = m_answer, m_cited or [best.ref], "model"
            except Exception as e:  # noqa: BLE001 - model down / invalid JSON: the curated answer, verbatim
                out["error"] = f"{type(e).__name__}: {e}"[:300]
        answer = cap_sentences(answer)
        o = self.sf.check_output(answer, secrets)
        if o is not None:
            out["problems"].append(f"output blocked: {o.category}")
            answer, cited, by = o.reply, [], "safety_filter"
        out.update(answer=answer, by=by, evidence=evidence_list(cited, passed))
        if by in ("model", "extract"):
            self._remember(conversation_id, q, answer)
        return done()


def viewable(path: str, kb: FrameworkKB | None = None) -> str | None:
    """Text of `path` when it is an indexed source or a file a curated answer cites (exact repo-relative match);
    None for anything else."""
    kb = kb or default_framework_kb()
    allowed = {c.source for c in kb.chunks} | {e.rsplit(":", 1)[0] for c in kb.chunks for e in c.evidence}
    if path not in allowed:
        return None
    p = kb.root / path
    return p.read_text(encoding="utf-8") if p.is_file() else None
