"""Deterministic factory quiz generated from warroom.factory (page 3 plant, page 6 schedule numbers).

Why: the hand-written quiz has 44 items, so one question is 2.27 points and the page-10 target
"NVFP4 vs FP8 gap ≤ 3 points" can only tolerate one question of difference — mostly noise. This adds
≥ 200 four-choice items whose answers are all in the open-book context the commander sees
(bench.prompts.factory_context + schedule_facts_context), so the open-book gap is measured on ~250 items.

  uv run python -m bench.quiz_gen            # rewrites bench/quiz/factory_quiz_generated.jsonl
"""
from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

from warroom import factory as F

OUT = Path(__file__).resolve().parent / "quiz" / "factory_quiz_generated.jsonl"
SIZE = {"S": "小胎", "M": "中胎", "L": "大胎"}


def _rng(key: str) -> random.Random:
    return random.Random(int(hashlib.sha256(key.encode()).hexdigest()[:12], 16))


def _item(qid: str, cat: str, question: str, correct: str, pool: list[str], source: str) -> dict[str, Any]:
    """4 choices: the correct one + 3 distinct distractors from pool, shuffled deterministically by id."""
    r = _rng(qid)
    distract = sorted({str(p) for p in pool if str(p) != str(correct)})
    if len(distract) < 3:
        raise ValueError(f"{qid}: need 3 distractors, have {distract}")
    choices = r.sample(distract, 3) + [str(correct)]
    r.shuffle(choices)
    return {"id": qid, "category": cat, "question": question, "choices": choices,
            "answer": "ABCD"[choices.index(str(correct))], "source": source}


def _hm(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return (f"T−{h} 時" + (f" {m} 分" if m else "")) if h else f"T−{m} 分"


def generate() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    add = items.append
    ms = list(F.MACHINES)
    line_labels = [F.LINE_LABELS[l] for l in F.LINE_LABELS]
    names = sorted({m.name for m in ms})
    processes = sorted({m.process for m in ms})
    ids = [m.id for m in ms]
    src = "warroom.factory (page 3)"

    for m in ms:
        add(_item(f"GQ-LINE-{m.id}", "機台", f"機台 {m.id} 屬於哪條產線？", F.LINE_LABELS[m.line], line_labels, src))
        add(_item(f"GQ-NAME-{m.id}", "機台", f"機台 {m.id} 是什麼設備？", m.name, names, src))
        add(_item(f"GQ-PROC-{m.id}", "製程", f"機台 {m.id} 負責哪一道製程？", m.process, processes, src))

    for m in ms:   # reverse of GQ-LINE: pick the machine on a named line
        if m.id.startswith("CURE-"):
            continue
        add(_item(f"GQ-ONLINE-{m.id}", "機台", f"下列哪台機台屬於「{F.LINE_LABELS[m.line]}」產線？", m.id,
                  [o.id for o in ms if o.line != m.line], src))

    # (line, process) -> machine id, where unique
    seen: dict[tuple[str, str], list[str]] = {}
    for m in ms:
        seen.setdefault((F.LINE_LABELS[m.line], m.process), []).append(m.id)
    for (lab, proc), mids in seen.items():
        if len(mids) == 1 and not mids[0].startswith("CURE-"):
            add(_item(f"GQ-WHO-{mids[0]}", "製程", f"「{lab}」產線的「{proc}」製程由哪台機台負責？", mids[0],
                      [i for i in ids if not i.startswith("CURE-")], src))

    compound = [m.id for m in ms if m.handles_compound]
    plain = [m.id for m in ms if not m.handles_compound]
    for mid in compound:
        add(_item(f"GQ-CMPD-{mid}", "安全", "下列哪台機台處理含膠料（受安全核心煉膠溫度規則保護）？", mid, plain, src))
    for mid in plain:
        if mid.startswith("CURE-"):
            continue
        add(_item(f"GQ-NOCMPD-{mid}", "安全", "下列哪台機台不處理含膠料？", mid, compound, src))

    presses = [m for m in ms if m.steam_header]
    headers = sorted({m.steam_header for m in presses})
    use = ["僅小胎", "中/大胎共用", "僅中胎", "僅大胎"]
    for m in presses:
        add(_item(f"GQ-HDR-{m.id}", "硫化", f"硫化機 {m.id} 接在哪一條蒸汽管線？", m.steam_header,
                  headers + ["H4", "H0"], src))
        add(_item(f"GQ-USE-{m.id}", "硫化", f"硫化機 {m.id} 可以硫化哪些尺寸？",
                  "僅小胎" if m.small_only else "中/大胎共用", use, src))
        others = [p.id for p in presses if p.steam_header != m.steam_header]
        add(_item(f"GQ-ONHDR-{m.id}", "硫化", f"下列哪台硫化機接在蒸汽管線 {m.steam_header}？", m.id, others, src))

    hours = [F.CURE_HOURS[k] for k in ("S", "M", "L")]
    hour_pool = [f"{h:g} 小時" for h in sorted(set(hours) | {12, 30, 36.5, 40, 48})]
    for k in ("S", "M", "L"):
        add(_item(f"GQ-CURE-{k}", "硫化", f"{SIZE[k]}的標準硫化時間是多少？", f"{F.CURE_HOURS[k]:g} 小時",
                  hour_pool, src))
    add(_item("GQ-CURE-TEMP", "硫化", "硫化機的正常溫度範圍是？",
              f"{F.CURE_TEMP_MIN_C:.0f}–{F.CURE_TEMP_MAX_C:.0f}°C",
              ["100–110°C", "110–120°C", "130–140°C", "140–150°C"], src))
    add(_item("GQ-CURE-PRESS", "硫化", "硫化時的壓力是？", f"{F.CURE_PRESSURE_KGF_CM2:.0f} kgf/cm²",
              ["5 kgf/cm²", "10 kgf/cm²", "35 kgf/cm²", "50 kgf/cm²"], src))
    add(_item("GQ-CURE-EA", "硫化", "硫化公式採用的活化能（假設值）是？", f"{F.ACTIVATION_ENERGY_KJ_MOL:g} kJ/mol",
              ["45 kJ/mol", "65 kJ/mol", "105 kJ/mol", "125 kJ/mol"], src))
    t_pool = ["100°C", "105°C", "110°C", "115°C", "120°C", "125°C", "130°C"]
    add(_item("GQ-SAFE-CMPD", "安全", "安全核心：未加硫的煉膠達到幾度就停機？", f"{F.COMPOUND_STOP_C:.0f}°C", t_pool, src))
    add(_item("GQ-SAFE-CUR", "安全", "安全核心：已加硫的膠料達到幾度就停機？", f"{F.CURATIVE_STOP_C:.0f}°C", t_pool, src))
    add(_item("GQ-SAFE-LOW", "安全", "安全核心：硫化機溫度低於幾度就保持（hold）？", f"{F.CURE_LOW_STOP_C:.0f}°C",
              t_pool, src))
    add(_item("GQ-NPRESS", "硫化", "工廠共有幾台硫化機？", str(len(presses)), ["7", "9", "10", "12", "28"], src))
    add(_item("GQ-NMACH", "機台", "工廠模型共有幾台機台？", str(len(ms)), ["11", "24", "26", "30", "32"], src))
    add(_item("GQ-NML", "硫化", "中/大胎共用的硫化機有幾台？", str(sum(not p.small_only for p in presses)),
              ["2", "3", "5", "7", "11"], src))

    src6 = "warroom.factory (page 6)"
    bp_pool = sorted({_hm(b[k]) for b in F.BACKPROP.values() for k in ("bond_start_min", "a_feed_min")}
                     | {_hm(30), _hm(120), _hm(180), _hm(240), _hm(50)})
    for k, b in F.BACKPROP.items():
        s = str(getattr(k, "value", k))
        add(_item(f"GQ-BP-BOND-{s}", "排程", f"{SIZE[s]}：入硫化機時刻為 T，應在何時開始貼合？",
                  _hm(b["bond_start_min"]), bp_pool, src6))
        add(_item(f"GQ-BP-FEED-{s}", "排程", f"{SIZE[s]}：入硫化機時刻為 T，A 線應在何時投料？",
                  _hm(b["a_feed_min"]), bp_pool, src6))
        add(_item(f"GQ-BP-PCS-{s}", "排程", f"{SIZE[s]}：胎側膠／鋼絲圈各需幾片？", f"{b['side_pieces']} 片",
                  [f"{n} 片" for n in (4, 6, 8, 10, 12)], src6))
    cap_pool_vals = sorted({v for r in F.CAPACITY_TARGETS for v in (r.small, r.medium, r.large)} | {4, 5.5, 8})
    for r in F.CAPACITY_TARGETS:
        for attr, s in (("small", "S"), ("medium", "M"), ("large", "L")):
            v = getattr(r, attr)
            add(_item(f"GQ-CAP-{r.label}-{s}", "排程", f"每台硫化機每週產量（{r.label}），{SIZE[s]}是幾條？",
                      f"{v:g}", [f"{x:g}" for x in cap_pool_vals], src6))
    ml, sm = F.LEAD_TIME_TARGET_WEEKS["ML_100_each"], F.LEAD_TIME_TARGET_WEEKS["small"]
    wk_pool = [f"{x:g} 週" for x in sorted({ml[0], ml[1], sm[0], sm[1], 1.5, 3.0, 7.5, 11.0, 15.0})]
    add(_item("GQ-LT-ML-NOW", "排程", "中、大胎各 100 條，現況交期是幾週？", f"{ml[0]:g} 週", wk_pool, src6))
    add(_item("GQ-LT-ML-AI", "排程", "中、大胎各 100 條，AI 排程後交期是幾週？", f"{ml[1]:g} 週", wk_pool, src6))
    add(_item("GQ-LT-S-NOW", "排程", "小胎現況交期是幾週？", f"{sm[0]:g} 週", wk_pool, src6))
    add(_item("GQ-LT-S-AI", "排程", "小胎 AI 排程後交期是幾週？", f"{sm[1]:g} 週", wk_pool, src6))

    span_pool = sorted({f"{s.start:%H:%M}–{s.end:%H:%M}" for s in F.OPTIMISED_SHIFTS}
                       | {"07:00–16:00", "22:00–07:00", "12:00–21:00", "18:00–03:00"})
    for s in F.OPTIMISED_SHIFTS:
        team = F.TEAM_LABELS[s.team]
        add(_item(f"GQ-SHIFT-{s.team.value}-{s.name}", "班表", f"（優化班表）{team}{s.name}的時段是？",
                  f"{s.start:%H:%M}–{s.end:%H:%M}", span_pool, src6))
    return items


def write(path: Path = OUT) -> int:
    items = generate()
    path.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items), encoding="utf-8")
    return len(items)


if __name__ == "__main__":
    print(write(), "items ->", OUT)
