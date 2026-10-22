"""QA 模式 eval: ask every question in tests/data/qa_eval.yaml and grade the answer by its key facts.

    uv run python scripts/qa_eval.py                        # mock mode: the curated answers, no model
    LLM_MODE=llm BASE_URL=http://127.0.0.1:11435/v1 MODEL=nemotron-3.5-lightning:30b-a3b-nvfp4 uv run python scripts/qa_eval.py
    ... --url http://127.0.0.1:8200                         # through a running server's POST /api/qa/ask

In scope: correct when every fact (a list of acceptable spellings) appears in the answer. Out of scope: correct when
the answer is the fallback (or a safety-filter line) and cites no evidence. Prints one line per question and the score;
--json writes the full transcript.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from warroom.agent.qa import FALLBACK

DATA = ROOT / "tests" / "data" / "qa_eval.yaml"


def grade(item: dict, res: dict) -> tuple[bool, list[str]]:
    answer = res.get("answer") or ""
    if item.get("out_of_scope"):
        ok = res.get("by") in ("fallback", "model_no_answer", "safety_filter") and not res.get("evidence")
        return ok, [] if ok else ["expected the fallback"]
    missing = [" / ".join(map(str, f)) for f in item["facts"] if not any(str(s).lower() in answer.lower() for s in f)]
    return not missing, missing


async def run_local(questions: list[dict]) -> list[dict]:
    from warroom.agent.qa import QaAgent
    from warroom.config import get_settings
    agent = QaAgent(get_settings())
    return [await agent.ask(it["q"]) for it in questions]


def run_http(url: str, questions: list[dict]) -> list[dict]:
    import httpx
    out = []
    with httpx.Client(base_url=url, timeout=180) as c:
        for it in questions:
            r = c.post("/api/qa/ask", json={"question": it["q"]})
            r.raise_for_status()
            out.append(r.json())
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="a running server (POST /api/qa/ask); default: in-process")
    ap.add_argument("--json", type=Path, help="write the transcript here")
    a = ap.parse_args()
    questions = yaml.safe_load(DATA.read_text(encoding="utf-8"))["questions"]
    t0 = time.perf_counter()
    results = run_http(a.url, questions) if a.url else asyncio.run(run_local(questions))
    n_ok = 0
    rows = []
    for it, res in zip(questions, results):
        ok, missing = grade(it, res)
        n_ok += ok
        rows.append({"q": it["q"], "ok": ok, "missing": missing, **{k: res.get(k) for k in ("answer", "by", "ms", "gate", "problems")},
                     "evidence": [e["ref"] for e in res.get("evidence") or []]})
        print(f"{'PASS' if ok else 'FAIL'} [{res.get('by')}] {res.get('ms')} ms  {it['q']}"
              + (f"  missing: {missing}" if missing else ""))
    score = n_ok / len(questions)
    print(f"\nscore {n_ok}/{len(questions)} = {score:.0%}  ({time.perf_counter() - t0:.0f} s)  fallback = {FALLBACK}")
    if a.json:
        a.json.write_text(json.dumps({"score": score, "n": len(questions), "rows": rows}, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    return 0 if score >= 0.9 else 1


if __name__ == "__main__":
    raise SystemExit(main())
