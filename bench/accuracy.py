"""NVFP4 vs FP8 accuracy on the factory quiz (page 10: 壓縮後準確率, gap ≤ 3 points).

Headline = OPEN BOOK (default): the prompt carries the factory model, the page-6 schedule numbers and the SOP
corpus, i.e. what the commander actually reads after sop_search. --closed-book tests the weights alone and is
reported separately, never as the headline.

Quiz = the hand-written kb quiz + the generated quiz (bench/quiz/factory_quiz_generated.jsonl, ≥ 200 items from
warroom.factory, `uv run python -m bench.quiz_gen`). On 44 items one question is 2.27 points, so a "≤ 3 point"
gap tolerated one question of noise; on ~250 items one question is ~0.4 points.
Quiz source, first match wins for the primary file (the seed set is a fallback), then the generated file is added:
  --quiz PATH | warroom/kb/quiz/*.jsonl|json | warroom/kb/*quiz*.jsonl|json | bench/quiz/factory_quiz_seed.jsonl
  --no-generated drops the generated file.
Accepted item shapes (JSONL rows or a JSON list / {"items": [...]}):
  {"id", "question"|"q", "choices"|"options": [..] | {"A": ..}, "answer": "B" | 1 | "<choice text>"}
  {"id", "question", "answer": "<text>", "keywords": [..]}            # free-form, keyword-scored

Statistics: both arms answer the same items, so the gap is a paired difference. The result carries the
discordant counts, an exact McNemar p-value and a 95 % Agresti-Min interval on (NVFP4 − reference) in points.
Verdicts: `*_gap` is the point estimate against ≤ 3 points (the proposal's wording); `*_noninferiority_ci95`
is PASS only if the whole interval stays above −3 points, FAIL if it lies wholly below, else n/a (too few items).

Quant labels: each arm is labelled from its model name (nvfp4 / fp8 / mxfp8 / bf16) or --quant. On the Mac the
reference arm is Ollama's ...-mxfp8: MXFP8 (8-bit microscaling), NOT the page-10 FP8 control, and the verdict
is named nvfp4_vs_mxfp8_*. The real control is vLLM QUANT=fp8 on the Spark (bench/spark_ablation.sh), whose two
saved single-model runs are paired afterwards with --pair.

Run one model:
  uv run python -m bench.accuracy --model nemotron-3.5-lightning:30b-a3b-nvfp4
Compare (each target is BASE_URL|MODEL; '|BASE' optional, defaults to BASE_URL env):
  uv run python -m bench.accuracy --compare "nemotron-3.5-lightning:30b-a3b-nvfp4" "nemotron-3.5-lightning:30b-a3b-mxfp8"
  uv run python -m bench.accuracy --compare "http://spark:8000/v1|nemotron" "http://spark:8001/v1|nemotron-fp8" --quant nvfp4 fp8
Pair two saved single-model runs (same quiz, same mode), e.g. the Spark ablation's full vs control:
  uv run python -m bench.accuracy --pair results/full/accuracy-*.json results/control/accuracy-*.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
from pathlib import Path
from typing import Any

from bench.llm import Endpoint, Verdict, endpoint_info, json_schema_format, parse_json, run, save_result, \
    stream_chat
from bench.prompts import SYSTEM, factory_context, schedule_facts_context, sop_context

ROOT = Path(__file__).resolve().parent.parent
SEED = Path(__file__).resolve().parent / "quiz" / "factory_quiz_seed.jsonl"
GENERATED = Path(__file__).resolve().parent / "quiz" / "factory_quiz_generated.jsonl"
GAP_TARGET_POINTS = 3.0
Z95 = 1.959963984540054
LETTERS = "ABCDEFGH"


def find_quiz(explicit: Path | None = None) -> Path:
    if explicit:
        return explicit
    cands: list[Path] = []
    for pat in ("warroom/kb/quiz/*.jsonl", "warroom/kb/quiz/*.json", "warroom/kb/*quiz*.jsonl",
                "warroom/kb/*quiz*.json", "bench/quiz/*.jsonl"):
        cands += sorted(p for p in ROOT.glob(pat) if p.name not in (SEED.name, GENERATED.name))
    return cands[0] if cands else SEED


def find_quizzes(explicit: list[Path] | None = None, generated: bool = True) -> list[Path]:
    """Primary quiz (explicit or find_quiz) plus the generated quiz unless disabled or already listed."""
    paths = list(explicit) if explicit else [find_quiz()]
    gen = ROOT / "bench" / "quiz" / GENERATED.name
    if generated and gen.exists() and all(p.resolve() != gen.resolve() for p in paths):
        paths.append(gen)
    return paths


def load_quizzes(paths: list[Path]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in paths:
        for it in load_quiz(p):
            if it["id"] in seen:
                it["id"] = f"{p.stem}:{it['id']}"
            seen.add(it["id"])
            items.append(it)
    return items


QUANTS = ("nvfp4", "mxfp8", "fp8", "bf16", "fp16", "int8", "int4", "q4_k_m", "q8_0")


def quant_of(model: str, override: str | None = None) -> str:
    """Quant label of an arm: --quant wins, else the first known token in the model name (mxfp8 before fp8)."""
    if override:
        return override.lower()
    m = model.lower()
    for q in QUANTS:
        if q in m:
            return q
    return "unknown"


QUANT_NOTE = {"mxfp8": "MXFP8 (Ollama/MLX 8-bit microscaling) is a Mac stand-in, not the page-10 FP8 control; "
                       "run vLLM QUANT=fp8 on the Spark (bench/spark_ablation.sh) for the real control"}


def _binom_two_sided(k: int, n: int) -> float:
    """Exact two-sided McNemar p-value: P(X ≤ min(k, n−k)) × 2 under Binomial(n, 0.5), capped at 1."""
    if n == 0:
        return 1.0
    lo = min(k, n - k)
    tail = sum(math.comb(n, i) for i in range(lo + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def paired_gap(a_items: list[dict[str, Any]], b_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Paired comparison of two graded runs on the same item ids. diff = a − b in points.
    CI: Agresti & Min (2005) adjusted Wald for a paired difference of proportions (adds 0.5 to each cell,
    so it does not collapse to zero width when there are no discordant items)."""
    b_by = {r["id"]: bool(r["correct"]) for r in b_items}
    pairs = [(bool(r["correct"]), b_by[r["id"]]) for r in a_items if r["id"] in b_by]
    n = len(pairs)
    both = sum(a and b for a, b in pairs)
    only_a = sum(a and not b for a, b in pairs)
    only_b = sum(b and not a for a, b in pairs)
    neither = n - both - only_a - only_b
    diff = 100 * (only_a - only_b) / max(n, 1)
    ns = n + 2
    p12, p21 = (only_a + 0.5) / ns, (only_b + 0.5) / ns
    d = p12 - p21
    se = math.sqrt(max((p12 + p21) - d * d, 0.0) / ns)
    lo, hi = 100 * max(-1.0, d - Z95 * se), 100 * min(1.0, d + Z95 * se)
    return {"n_paired": n, "both_correct": both, "only_a_correct": only_a, "only_b_correct": only_b,
            "neither_correct": neither, "diff_points": round(diff, 2), "gap_points": round(abs(diff), 2),
            "ci95_points": [round(lo, 2), round(hi, 2)], "ci_method": "Agresti-Min adjusted Wald (paired)",
            "mcnemar_exact_p": round(_binom_two_sided(only_a, only_a + only_b), 4),
            "points_per_item": round(100 / max(n, 1), 2)}


def compare_verdicts(a: dict[str, Any], b: dict[str, Any], mode: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """a = NVFP4 arm (production), b = reference arm. Returns (paired stats, verdict dicts)."""
    st = paired_gap(a["items"], b["items"])
    qa, qb = a.get("quant") or "a", b.get("quant") or "b"
    name = f"{qa}_vs_{qb}"
    lo, hi = st["ci95_points"]
    base = (f"{mode}; {a['endpoint']['model']} [{qa.upper()}]={a['score_points']} vs "
            f"{b['endpoint']['model']} [{qb.upper()}]={b['score_points']} on n={st['n_paired']} "
            f"(1 item = {st['points_per_item']} pts)")
    if qb in QUANT_NOTE:
        base += "; " + QUANT_NOTE[qb]
    gap = st["gap_points"]
    noninf = True if lo >= -GAP_TARGET_POINTS else (False if hi < -GAP_TARGET_POINTS else None)
    verdicts = [
        Verdict(f"{name}_gap", f"≤ {GAP_TARGET_POINTS} points", gap, gap <= GAP_TARGET_POINTS, note=base,
                extra={k: st[k] for k in ("diff_points", "ci95_points", "mcnemar_exact_p")}).__dict__,
        Verdict(f"{name}_noninferiority_ci95", f"95% CI of {qa}−{qb} > −{GAP_TARGET_POINTS} points",
                [lo, hi], noninf,
                note=(f"diff {st['diff_points']:+} pts, discordant {st['only_a_correct']}/{st['only_b_correct']}, "
                      f"McNemar p={st['mcnemar_exact_p']}"
                      + ("" if noninf is not None else "; inconclusive: CI straddles −3, needs more items"))).__dict__,
    ]
    return st, verdicts


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
    i.e. what the commander sees after sop_search (tests reading, the production condition). Open book also
    carries the page-6 schedule numbers the solver/orders tools return, so every quiz item has its source
    in the prompt (before 2026-10-03 FQ-024 '13.3 → 9.3 週' had none)."""
    if not open_book:
        return SYSTEM + "\n\n" + factory_context()
    return SYSTEM + "\n\n" + factory_context() + "\n\n" + schedule_facts_context() + "\n\n" + sop_context()


CITE_HINT = "先在 evidence 引用上文中能回答本題的原句（≤40 字），再給答案字母"


def mc_messages(item: dict[str, Any], open_book: bool = False, cite: bool = False) -> list[dict[str, str]]:
    opts = "\n".join(f"{LETTERS[i]}. {c}" for i, c in enumerate(item["choices"]))
    fmt = (f"{CITE_HINT}。只輸出 JSON：{{\"evidence\": \"原句\", \"answer\": \"字母\"}}" if cite
           else "只輸出 JSON：{\"answer\": \"字母\"}")
    return [{"role": "system", "content": system_prompt(open_book)},
            {"role": "user", "content": f"題目：{item['question']}\n{opts}\n\n{fmt}"}]


def mc_schema(n: int, cite: bool = False) -> dict[str, Any]:
    props: dict[str, Any] = {"answer": {"type": "string", "enum": list(LETTERS[:n])}}
    if cite:   # evidence first: the model quotes its source before it commits to a letter
        props = {"evidence": {"type": "string", "maxLength": 120}, **props}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def free_messages(item: dict[str, Any], open_book: bool = False) -> list[dict[str, str]]:
    return [{"role": "system", "content": system_prompt(open_book)},
            {"role": "user", "content": f"題目：{item['question']}\n請用一到兩句話回答。"}]


def score_free(text: str, item: dict[str, Any]) -> bool:
    kws = item.get("keywords") or ([str(item["answer"])] if item.get("answer") is not None else [])
    norm = re.sub(r"\s+", "", text)
    return bool(kws) and all(re.sub(r"\s+", "", str(k)) in norm for k in kws)


async def grade(ep: Endpoint, items: list[dict[str, Any]], concurrency: int = 2,
                think: bool = False, open_book: bool = False, cite: bool = False) -> dict[str, Any]:
    sem = asyncio.Semaphore(concurrency)
    cli = ep.client(timeout_s=300)

    async def one(item):
        async with sem:
            if item.get("choices"):
                schema = mc_schema(len(item["choices"]), cite)
                r = await stream_chat(ep, mc_messages(item, open_book, cite),
                                      max_tokens=2048 if think else (200 if cite else 20),
                                      response_format=json_schema_format("QuizAnswer", schema), think=think,
                                      client=cli)
                got = parse_json(r.text)
                letter = (got or {}).get("answer") if isinstance(got, dict) else None
                evidence = (got or {}).get("evidence") if isinstance(got, dict) else None
                if letter is None:
                    m = re.search(r"\b([A-H])\b", r.text)
                    letter = m.group(1) if m else None
                return {"id": item["id"], "ok": r.ok, "got": letter, "expected": item.get("answer_letter"),
                        "correct": letter is not None and letter == item.get("answer_letter"),
                        "latency_s": round(r.total_s, 3), "error": r.error,
                        **({"evidence": evidence} if cite else {})}
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


def _rel(p: Path) -> str:
    return str(p.relative_to(ROOT) if p.is_relative_to(ROOT) else p)


def _mode(open_book: bool) -> str:
    return "open-book (headline)" if open_book else "closed-book"


async def main_async(args) -> dict[str, Any]:
    if args.pair:
        return pair_saved(args.pair[0], args.pair[1], args.out_dir)
    quizzes = find_quizzes(args.quiz, generated=not args.no_generated)
    items = load_quizzes(quizzes)
    if args.limit:
        items = items[:args.limit]
    targets = [parse_target(t) for t in args.compare] if args.compare else [Endpoint.from_env(args.base_url, args.model)]
    quant_over = list(args.quant or []) + [None] * len(targets)
    out: dict[str, Any] = {"quiz": ", ".join(_rel(q) for q in quizzes), "quiz_files": [_rel(q) for q in quizzes],
                           "n_items": len(items), "think": args.think, "open_book": args.open_book,
                           "mode": _mode(args.open_book), "cite": args.cite,
                           "points_per_item": round(100 / max(len(items), 1), 2), "results": []}
    for ep, q in zip(targets, quant_over):
        await stream_chat(ep, [{"role": "user", "content": "hi"}], max_tokens=1)  # load model
        g = await grade(ep, items, args.concurrency, args.think, args.open_book, args.cite)
        out["results"].append({"endpoint": await endpoint_info(ep), "quant": quant_of(ep.model, q), **g})
    if len(out["results"]) == 2:
        a, b = out["results"]
        out["paired"], out["verdicts"] = compare_verdicts(a, b, out["mode"])
        out["gap_points"] = out["paired"]["gap_points"]
    tag = "compare" + ("-open-book" if args.open_book else "-closed-book") + ("-cite" if args.cite else "")
    out["saved_to"] = str(save_result("accuracy", targets[0] if len(targets) == 1 else None, out, args.out_dir,
                                      tag=tag if len(targets) == 2 else None))
    return out


def pair_saved(path_a: Path, path_b: Path, out_dir: Path | None = None) -> dict[str, Any]:
    """Pair two saved single-model runs (e.g. Spark full=NVFP4 vs control=FP8, run at different times)."""
    da, db = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (path_a, path_b))
    for k in ("quiz", "open_book", "think", "cite"):
        if da.get(k) != db.get(k):
            raise SystemExit(f"cannot pair: '{k}' differs ({da.get(k)!r} vs {db.get(k)!r})")
    a, b = da["results"][0], db["results"][0]
    a.setdefault("quant", quant_of(a["endpoint"]["model"]))
    b.setdefault("quant", quant_of(b["endpoint"]["model"]))
    out: dict[str, Any] = {k: da.get(k) for k in ("quiz", "quiz_files", "n_items", "think", "open_book", "cite")}
    out |= {"mode": _mode(bool(da.get("open_book"))), "points_per_item": da.get("points_per_item"),
            "paired_from": [str(path_a), str(path_b)], "results": [a, b]}
    out["paired"], out["verdicts"] = compare_verdicts(a, b, out["mode"])
    out["gap_points"] = out["paired"]["gap_points"]
    tag = "compare" + ("-open-book" if da.get("open_book") else "-closed-book") + "-paired"
    out["saved_to"] = str(save_result("accuracy", None, out, out_dir, tag=tag))
    return out


def main(argv: list[str] | None = None) -> dict[str, Any]:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url")
    p.add_argument("--model")
    p.add_argument("--compare", nargs=2, metavar="TARGET")
    p.add_argument("--quiz", type=Path, action="append", help="quiz file (repeatable); default kb quiz")
    p.add_argument("--no-generated", action="store_true", help="do not add bench/quiz/factory_quiz_generated.jsonl")
    p.add_argument("--quant", nargs="+", metavar="LABEL",
                   help="quant label per target (nvfp4 fp8 ...); default: parsed from the model name")
    p.add_argument("--pair", nargs=2, type=Path, metavar="RESULT_JSON",
                   help="pair two saved single-model results (A = NVFP4, B = reference) instead of running")
    p.add_argument("--limit", type=int)
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--think", action="store_true")
    book = p.add_mutually_exclusive_group()
    book.add_argument("--open-book", dest="open_book", action="store_true", default=True,
                      help="(default) factory model + schedule numbers + SOP corpus in the prompt: the headline")
    book.add_argument("--closed-book", dest="open_book", action="store_false",
                      help="factory model only: what the weights know (secondary)")
    p.add_argument("--cite", action="store_true",
                   help="evidence-first answer: quote the source line, then the letter (same for both models)")
    p.add_argument("--out-dir", type=Path)
    out = run(main_async(p.parse_args(argv)))
    print("mode", out["mode"])
    for r in out["results"]:
        print(f"{r['endpoint']['model']} [{r.get('quant', '?')}]: {r['correct']}/{r['n']} = {r['score_points']} points "
              f"(errors {r['errors']})")
    if "paired" in out:
        st = out["paired"]
        print(f"gap {st['gap_points']} points (target ≤ {GAP_TARGET_POINTS}); diff {st['diff_points']:+}, "
              f"95% CI {st['ci95_points']}, discordant {st['only_a_correct']}/{st['only_b_correct']}, "
              f"McNemar p={st['mcnemar_exact_p']}")
        for v in out["verdicts"]:
            print(f"  {v['name']}: {v['passed']}  {v['note']}")
    print("quiz", out["quiz"], "saved", out["saved_to"])
    return out


if __name__ == "__main__":
    main()
