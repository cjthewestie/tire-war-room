"""NVFP4 vs FP8 accuracy on the factory quiz (page 10: 壓縮後準確率, gap ≤ 3 points).

Quiz source, first match wins (the kb builder owns the real quiz; the seed set is a fallback):
  --quiz PATH | warroom/kb/quiz/*.jsonl|json | warroom/kb/*quiz*.jsonl|json | bench/quiz/factory_quiz_seed.jsonl
Accepted item shapes (JSONL rows or a JSON list / {"items": [...]}):
  {"id", "question"|"q", "choices"|"options": [..] | {"A": ..}, "answer": "B" | 1 | "<choice text>"}
  {"id", "question", "answer": "<text>", "keywords": [..]}            # free-form, keyword-scored

Run one model:
  uv run python -m bench.accuracy --model nemotron-3.5-lightning:30b-a3b-nvfp4
Compare (each target is BASE_URL|MODEL; '|BASE' optional, defaults to BASE_URL env):
  uv run python -m bench.accuracy --compare "nemotron-3.5-lightning:30b-a3b-nvfp4" "nemotron-3.5-lightning:30b-a3b-mxfp8"
  uv run python -m bench.accuracy --compare "http://spark:8000/v1|nemotron" "http://spark:8001/v1|nemotron-fp8"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import Any

from bench.llm import Endpoint, Verdict, endpoint_info, json_schema_format, parse_json, run, save_result, \
    stream_chat
from bench.prompts import SYSTEM, factory_context, sop_context

ROOT = Path(__file__).resolve().parent.parent
SEED = Path(__file__).resolve().parent / "quiz" / "factory_quiz_seed.jsonl"
GAP_TARGET_POINTS = 3.0
LETTERS = "ABCDEFGH"


def find_quiz(explicit: Path | None = None) -> Path:
    if explicit:
        return explicit
    cands: list[Path] = []
    for pat in ("warroom/kb/quiz/*.jsonl", "warroom/kb/quiz/*.json", "warroom/kb/*quiz*.jsonl",
                "warroom/kb/*quiz*.json", "bench/quiz/*.jsonl"):
        cands += sorted(p for p in ROOT.glob(pat) if p != SEED)
    return cands[0] if cands else SEED


def load_quiz(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if path.suffix == ".jsonl":
        raw = [json.loads(l) for l in text.splitlines() if l.strip()]
    else:
        d = json.loads(text)
        raw = d.get("items") or d.get("questions") or [] if isinstance(d, dict) else d
    items = []
    for i, r in enumerate(raw):
        q = r.get("question") or r.get("q")
        choices = r.get("choices") or r.get("options")
        if isinstance(choices, dict):
            choices = [choices[k] for k in sorted(choices)]
        ans = r.get("answer")
        item = {"id": str(r.get("id", f"q{i + 1:02d}")), "question": q, "choices": choices, "answer": ans,
                "keywords": r.get("keywords") or r.get("answer_keywords")}
        if choices:
            item["answer_letter"] = normalize_answer(ans, choices)
        items.append(item)
    return [x for x in items if x["question"]]


def normalize_answer(ans: Any, choices: list[str]) -> str | None:
    if isinstance(ans, int):
        return LETTERS[ans] if 0 <= ans < len(choices) else None
    if isinstance(ans, str):
        a = ans.strip()
        if len(a) == 1 and a.upper() in LETTERS[:len(choices)]:
            return a.upper()
        for i, c in enumerate(choices):
            if a == str(c).strip():
                return LETTERS[i]
    return None


def system_prompt(open_book: bool) -> str:
    """Closed book = factory model only (tests what the weights know). Open book = + the SOP corpus,
    i.e. what the commander sees after sop_search (tests reading, the production condition)."""
    return SYSTEM + "\n\n" + factory_context() + ("\n\n" + sop_context() if open_book else "")


def mc_messages(item: dict[str, Any], open_book: bool = False) -> list[dict[str, str]]:
    opts = "\n".join(f"{LETTERS[i]}. {c}" for i, c in enumerate(item["choices"]))
    return [{"role": "system", "content": system_prompt(open_book)},
            {"role": "user", "content": f"題目：{item['question']}\n{opts}\n\n只輸出 JSON：{{\"answer\": \"字母\"}}"}]


def free_messages(item: dict[str, Any], open_book: bool = False) -> list[dict[str, str]]:
    return [{"role": "system", "content": system_prompt(open_book)},
            {"role": "user", "content": f"題目：{item['question']}\n請用一到兩句話回答。"}]


def score_free(text: str, item: dict[str, Any]) -> bool:
    kws = item.get("keywords") or ([str(item["answer"])] if item.get("answer") is not None else [])
    norm = re.sub(r"\s+", "", text)
    return bool(kws) and all(re.sub(r"\s+", "", str(k)) in norm for k in kws)


async def grade(ep: Endpoint, items: list[dict[str, Any]], concurrency: int = 2,
                think: bool = False, open_book: bool = False) -> dict[str, Any]:
    sem = asyncio.Semaphore(concurrency)
    cli = ep.client(timeout_s=300)

    async def one(item):
        async with sem:
            if item.get("choices"):
                n = len(item["choices"])
                schema = {"type": "object", "properties": {"answer": {"type": "string", "enum": list(LETTERS[:n])}},
                          "required": ["answer"], "additionalProperties": False}
                r = await stream_chat(ep, mc_messages(item, open_book), max_tokens=2048 if think else 20,
                                      response_format=json_schema_format("QuizAnswer", schema), think=think,
                                      client=cli)
                got = parse_json(r.text)
                letter = (got or {}).get("answer") if isinstance(got, dict) else None
                if letter is None:
                    m = re.search(r"\b([A-H])\b", r.text)
                    letter = m.group(1) if m else None
                return {"id": item["id"], "ok": r.ok, "got": letter, "expected": item.get("answer_letter"),
                        "correct": letter is not None and letter == item.get("answer_letter"),
                        "latency_s": round(r.total_s, 3), "error": r.error}
            r = await stream_chat(ep, free_messages(item, open_book), max_tokens=200, think=think, client=cli)
            return {"id": item["id"], "ok": r.ok, "got": r.text[:300], "expected": item.get("answer"),
                    "correct": score_free(r.text, item), "latency_s": round(r.total_s, 3), "error": r.error}

    res = await asyncio.gather(*(one(i) for i in items))
    await cli.close()
    n = len(res)
    correct = sum(r["correct"] for r in res)
    return {"n": n, "correct": correct, "score_points": round(100 * correct / max(n, 1), 2),
            "errors": sum(not r["ok"] for r in res), "items": res}


def parse_target(s: str) -> Endpoint:
    if "|" in s:
        base, model = s.split("|", 1)
        return Endpoint.from_env(base, model)
    return Endpoint.from_env(None, s)


async def main_async(args) -> dict[str, Any]:
    quiz = find_quiz(args.quiz)
    items = load_quiz(quiz)
    if args.limit:
        items = items[:args.limit]
    targets = [parse_target(t) for t in args.compare] if args.compare else [Endpoint.from_env(args.base_url, args.model)]
    out: dict[str, Any] = {"quiz": str(quiz.relative_to(ROOT) if quiz.is_relative_to(ROOT) else quiz),
                           "n_items": len(items), "think": args.think, "open_book": args.open_book,
                           "points_per_item": round(100 / max(len(items), 1), 2), "results": []}
    for ep in targets:
        await stream_chat(ep, [{"role": "user", "content": "hi"}], max_tokens=1)  # load model
        g = await grade(ep, items, args.concurrency, args.think, args.open_book)
        out["results"].append({"endpoint": await endpoint_info(ep), **g})
    if len(out["results"]) == 2:
        a, b = out["results"]
        gap = round(abs(a["score_points"] - b["score_points"]), 2)
        out["gap_points"] = gap
        out["verdicts"] = [Verdict("nvfp4_vs_fp8_gap", f"≤ {GAP_TARGET_POINTS} points", gap,
                                   gap <= GAP_TARGET_POINTS,
                                   note=f"{a['endpoint']['model']}={a['score_points']} vs "
                                        f"{b['endpoint']['model']}={b['score_points']} on n={len(items)}").__dict__]
    tag = "compare-open-book" if args.open_book else "compare"
    out["saved_to"] = str(save_result("accuracy", targets[0] if len(targets) == 1 else None, out, args.out_dir,
                                      tag=tag if len(targets) == 2 else None))
    return out


def main(argv: list[str] | None = None) -> dict[str, Any]:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url")
    p.add_argument("--model")
    p.add_argument("--compare", nargs=2, metavar="TARGET")
    p.add_argument("--quiz", type=Path)
    p.add_argument("--limit", type=int)
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--think", action="store_true")
    p.add_argument("--open-book", action="store_true", help="include the SOP corpus in the prompt")
    p.add_argument("--out-dir", type=Path)
    out = run(main_async(p.parse_args(argv)))
    for r in out["results"]:
        print(f"{r['endpoint']['model']}: {r['correct']}/{r['n']} = {r['score_points']} points "
              f"(errors {r['errors']})")
    if "gap_points" in out:
        print(f"gap {out['gap_points']} points (target ≤ {GAP_TARGET_POINTS})")
    print("quiz", out["quiz"], "saved", out["saved_to"])
    return out


if __name__ == "__main__":
    main()
