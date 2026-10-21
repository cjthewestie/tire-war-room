"""Spark 10-21 §5: which presses take medium / large tyres was stated three ways (commander CURE-08..11, PLANT.md /
ask / SOPs 7–10). factory.py is the source of truth (ML_PRESSES); every prompt, PLANT.md and SOP text must agree."""
from __future__ import annotations

import re
from pathlib import Path

from warroom import factory as F
from warroom.agent.ask import PLAN_SYSTEM
from warroom.agent.commander import SYSTEM_PROMPT

ROOT = Path(__file__).resolve().parents[1]
NUMS = [int(m[5:]) for m in F.ML_PRESSES]


def _range(a: str, b: str) -> list[int]:
    return list(range(int(a), int(b) + 1))


def test_ml_range_follows_the_press_list():
    assert NUMS == _range(*F.ML_RANGE.split("–"))       # contiguous, and the text says exactly that
    assert F.ML_RANGE == "8–11"


def test_prompts_agree_with_factory():
    m = re.search(r"CURE-(\d+)\.\.(\d+) 可做中／大胎，CURE-(\d+)\.\.(\d+) 只做小胎", SYSTEM_PROMPT)
    assert m and _range(m[1], m[2]) == NUMS
    assert [int(x[5:]) for x in F.SMALL_ONLY_PRESSES] == _range(m[3], m[4])
    m = re.search(r"（(\d+)–(\d+) 號可做中大胎）", PLAN_SYSTEM)
    assert m and _range(m[1], m[2]) == NUMS


def test_plant_md_and_sops_agree_with_factory():
    plant = (ROOT / "config/plant/PLANT.md").read_text(encoding="utf-8")
    m = re.search(r"(\d+)–(\d+) 號可做中、大胎", plant)
    assert m and _range(m[1], m[2]) == NUMS
    found = 0
    for p in sorted((ROOT / "warroom/kb/sop").glob("*.md")):
        text = p.read_text(encoding="utf-8")
        for a, b in re.findall(r"(\d+)～(\d+) 號(?:共用機|）只做中、大胎)", text) + \
                re.findall(r"（(\d+)～(\d+) 號）只做中、大胎", text):
            assert _range(a, b) == NUMS, f"{p.name}: {a}～{b}"
            found += 1
        for a, b in re.findall(r"小胎機台（(\d+)～(\d+) 號）", text):
            assert [n for n in _range(a, b)] == [int(x[5:]) for x in F.SMALL_ONLY_PRESSES], p.name
    assert found >= 3                       # cure-interrupt, mold-change, weekend-no-fireout
