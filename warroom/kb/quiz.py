"""Factory quiz for the page-10 accuracy check: NVFP4 vs FP8 must differ by ≤3 points.

warroom/kb/quiz/factory_quiz.jsonl holds multiple-choice questions (Traditional Chinese) built
from the proposal and the SOP corpus; each line: id, category, question, choices{A..D}, answer,
explanation, source. bench/ asks the model each question with `prompt()`, extracts the letter
with `parse_choice()`, and compares runs with `score()`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

QUIZ_PATH = Path(__file__).resolve().parent / "quiz" / "factory_quiz.jsonl"
SYSTEM_PROMPT = "你是實心胎工廠的製程專家。請只回答一個選項字母（A、B、C 或 D），不要解釋。"
_LETTER = re.compile(r"(?<![A-Za-z])([ABCD])(?![A-Za-z])")


def load_quiz(path: Path | None = None) -> list[dict[str, Any]]:
    with open(path or QUIZ_PATH, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def prompt(item: dict[str, Any]) -> str:
    opts = "\n".join(f"{k}. {v}" for k, v in item["choices"].items())
    return f"{item['question']}\n{opts}\n答案："


def parse_choice(text: str) -> str | None:
    """First standalone A–D in the model output ("答案：C" / "C." / "(B)"); None if absent."""
    if not text:
        return None
    text = text.replace("Ａ", "A").replace("Ｂ", "B").replace("Ｃ", "C").replace("Ｄ", "D")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    m = _LETTER.search(text.upper())
    return m.group(1) if m else None


def score(answers: dict[str, str | None], quiz: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """answers: {question id: letter or raw model text}. Returns accuracy in points (0–100) overall
    and per category, plus the ids answered wrong."""
    quiz = quiz or load_quiz()
    by_cat: dict[str, list[int]] = {}
    wrong = []
    for item in quiz:
        raw = answers.get(item["id"])
        got = raw if raw in ("A", "B", "C", "D") else parse_choice(raw or "")
        ok = int(got == item["answer"])
        by_cat.setdefault(item["category"], []).append(ok)
        if not ok:
            wrong.append(item["id"])
    total = sum(sum(v) for v in by_cat.values())
    n = sum(len(v) for v in by_cat.values())
    return {"n": n, "correct": total, "accuracy_pts": round(100.0 * total / n, 2) if n else 0.0,
            "by_category": {k: round(100.0 * sum(v) / len(v), 2) for k, v in by_cat.items()}, "wrong": wrong}
