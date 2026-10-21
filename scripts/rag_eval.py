"""SOP knowledge-base eval: does sop_search name the right SOP for an in-KB question, and say nothing
for an out-of-KB one? Reads tests/data/rag_eval.yaml, runs every question through sop_search_tool
(the same function the agents call), and prints a score table plus precision, recall and accuracy.

    uv run python scripts/rag_eval.py            # table + totals
    uv run python scripts/rag_eval.py --curve    # also sweep Settings.kb_min_score over the eval set

A returned hit is a positive. Precision = right top hits / all returned hits; recall = right top hits /
in-KB questions; accuracy = (right in-KB + empty out-of-KB) / all questions (the ≥ 90 % bar).
The index is built in data/runtime/qdrant_eval (embedded Qdrant) so the live index is never touched.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from warroom.kb.store import QdrantSopKB
from warroom.kb.tools import sop_search_tool

EVAL = ROOT / "tests" / "data" / "rag_eval.yaml"


def load() -> tuple[list[dict], list[str]]:
    data = yaml.safe_load(EVAL.read_text(encoding="utf-8"))
    return data["in_kb"], data["out_of_kb"]


def run(kb: QdrantSopKB, in_kb: list[dict], out_kb: list[str], verbose: bool = True) -> dict[str, float]:
    rows, right_in, empty_out, returned = [], 0, 0, 0
    for item in in_kb:
        res = sop_search_tool(SimpleNamespace(kb=kb), {"query": item["q"], "k": 3})
        hits = res["hits"]
        top = hits[0] if hits else None
        ok = bool(top) and top["sop_id"] in item["expect"]
        returned += bool(top)
        right_in += ok
        rows.append(("in ", item["q"], "/".join(item["expect"]), top, ok, res.get("reason", "")))
    for q in out_kb:
        res = sop_search_tool(SimpleNamespace(kb=kb), {"query": q, "k": 3})
        hits = res["hits"]
        top = hits[0] if hits else None
        returned += bool(top)
        empty_out += not top
        rows.append(("out", q, "（無）", top, not top, res.get("reason", "")))
    total = len(in_kb) + len(out_kb)
    m = {"precision": right_in / returned if returned else 1.0, "recall": right_in / len(in_kb),
         "accuracy": (right_in + empty_out) / total, "in_kb": f"{right_in}/{len(in_kb)}",
         "out_of_kb": f"{empty_out}/{len(out_kb)}"}
    if verbose:
        print(f"{'':3} {'pass':4} {'query':18} {'expect':22} {'top hit':12} {'source':12} {'score':>6}  reason")
        for kind, q, exp, top, ok, reason in rows:
            sid, src, sc = (top["sop_id"], top.get("source_label", top["source"])[:10], f"{top['score']:.3f}") \
                if top else ("—", "", "")
            print(f"{kind} {'ok' if ok else 'FAIL':4} {q:18} {exp:22} {sid:12} {src:12} {sc:>6}  {reason}")
        print(f"\nprecision {m['precision']:.2%}  recall {m['recall']:.2%}  accuracy {m['accuracy']:.2%}"
              f"  (in-KB right {m['in_kb']}, out-of-KB empty {m['out_of_kb']})")
    return m


def curve(kb: QdrantSopKB, in_kb: list[dict], out_kb: list[str]) -> None:
    """Accuracy at each kb_min_score with the key-term check on (what ships), and score-only (the check off:
    strong_score lowered to the threshold, so the score alone decides) for comparison."""
    if not hasattr(kb, "min_score"):
        print("\n(no kb.min_score on this build: no curve)")
        return
    keep, strong = kb.min_score, kb.strong_score
    print("\n           --- score + key term (ships) ---------------   --- score only ---")
    print("threshold  accuracy  precision  recall   in-KB  out-of-KB   accuracy  in-KB  out-of-KB")
    for t in [x / 100 for x in range(10, 62, 2)]:
        kb.min_score, kb.strong_score = t, strong
        m = run(kb, in_kb, out_kb, verbose=False)
        kb.strong_score = t
        s = run(kb, in_kb, out_kb, verbose=False)
        mark = "  <- Settings.kb_min_score" if abs(t - keep) < 1e-9 else ""
        print(f"  {t:.2f}     {m['accuracy']:.2%}   {m['precision']:.2%}   {m['recall']:.2%}  "
              f"{m['in_kb']:>6}  {m['out_of_kb']:>6}     {s['accuracy']:.2%}  {s['in_kb']:>6}  {s['out_of_kb']:>6}{mark}")
    kb.min_score, kb.strong_score = keep, strong


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--curve", action="store_true")
    a = ap.parse_args()
    kb = QdrantSopKB(path=ROOT / "data" / "runtime" / "qdrant_eval", collection="sop_eval")
    kb.ingest()
    print(f"mode {kb.mode}, team {kb.team_mode}\n")
    in_kb, out_kb = load()
    run(kb, in_kb, out_kb)
    if a.curve:
        curve(kb, in_kb, out_kb)


if __name__ == "__main__":
    main()
