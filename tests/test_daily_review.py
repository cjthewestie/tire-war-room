"""warroom.tools.daily_review (issue #44): read-only report from a synthetic audit log."""
from __future__ import annotations

import json
from pathlib import Path

from warroom.tools import daily_review as dr


def _write(path: Path, rows: list[tuple]) -> None:
    lines = []
    for i, (kind, actor, eid, payload, ts) in enumerate(rows, start=1):
        lines.append(json.dumps({"seq": i, "ts": ts, "kind": kind, "actor": actor, "event_id": eid,
                                 "payload": payload, "prev_hash": "x", "hash": "y"}, ensure_ascii=False))
    lines.insert(3, "this line is not json")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _log(tmp_path: Path) -> Path:
    d1, d2 = "2026-10-22T14:26:00+08:00", "2026-10-23T09:00:00+08:00"
    st = lambda eid, frm, to, why, ts=d1: ("state_transition", "state_machine", eid,
                                           {"from": frm, "to": to, "reason": why}, ts)
    an = lambda eid, mid, kind, ts=d1: ("anomaly", "detector", eid,
                                        {"machine_id": mid, "kind": kind, "message": "m"}, ts)
    llm = lambda eid, code, ts=d1: ("llm_call", "commander", eid,
                                    {"phase": "plan", "plan_source": "commander", "cause_code": code}, ts)
    rows = [
        # E1: detector, CURE-07, H1 overturned -> H2, supervisor rejected then modified, closed
        an("EV-1", "CURE-07", "steam_low"), st("EV-1", None, "detected", "detector wake-up"),
        st("EV-1", "detected", "classify", ""), llm("EV-1", "steam_pressure_low"),
        ("approval_decision", "supervisor", "EV-1", {"decision": "reject", "request_id": "A1"}, d1),
        st("EV-1", "classify", "replan", "主管否決 → 換方案"),
        ("system", "verifier", "EV-1", {"hypothesis": "H1", "ok": False}, d1), llm("EV-1", "steam_leak"),
        ("approval_decision", "supervisor", "EV-1", {"decision": "modify", "request_id": "A2"}, d1),
        st("EV-1", "replan", "closed", "verified"),
        # E2: same machine again the same day, clean
        an("EV-2", "CURE-07", "steam_low"), st("EV-2", None, "detected", "detector wake-up"),
        llm("EV-2", "steam_pressure_low"), st("EV-2", "detected", "closed", "verified"),
        # E3: supervisor command, discussion unclear, safe stop
        st("EV-3", None, "detected", "supervisor command"),
        ("system", "agent", "EV-3", {"discuss_reply": {"id": "DIS-1", "intent": "unclear"}}, d1),
        st("EV-3", "detected", "safe_stop", "連續失敗"),
        # E5: p.11 style: round 1 approve option B, round 2 approve option A (supervisor changed the choice)
        an("EV-5", "CURE-09", "temp_drop"), st("EV-5", None, "detected", "detector wake-up"),
        ("approval_decision", "supervisor", "EV-5", {"decision": "approve", "option_id": "B", "round": 1}, d1),
        ("approval_decision", "supervisor", "EV-5", {"decision": "approve", "option_id": "A", "round": 2}, d1),
        st("EV-5", "detected", "closed", "verified"),
        # E4: next day, must not show up for --date 2026-10-22
        an("EV-4", "A-MIX", "overheat", d2), st("EV-4", None, "detected", "detector wake-up", d2),
        # an "ask" conversation is not an event
        ("system", "ask", "ask-1", {"msg": "ask", "question": "hi"}, d1),
    ]
    p = tmp_path / "audit.jsonl"
    _write(p, rows)
    return p


def test_reviews_flags_and_filters(tmp_path):
    entries, skipped = dr.read_entries(_log(tmp_path))
    assert skipped == 1
    rv = {r.event_id: r for r in dr.build_reviews(entries, "2026-10-22")}
    assert set(rv) == {"EV-1", "EV-2", "EV-3", "EV-5"}            # next-day event and the ask are out

    e1 = rv["EV-1"]
    assert (e1.machine_id, e1.source, e1.ended) == ("CURE-07", "偵測器", "結案")
    assert e1.cause_codes == ["steam_pressure_low", "steam_leak"]
    assert (e1.replans, e1.hypotheses_overturned, e1.rejects, e1.modifies) == (1, 1, 1, 1)
    assert {"repeat_machine", "hypothesis_swap", "supervisor_veto"} <= set(e1.flags)

    assert rv["EV-2"].flags == ["repeat_machine"]
    e5 = rv["EV-5"]                                   # same option twice would not count; B then A does
    assert (e5.rejects, e5.modifies) == (0, 1) and e5.flags == ["supervisor_veto"]
    e3 = rv["EV-3"]
    assert e3.source == "主管指令" and e3.ended == "安全中止"
    assert set(e3.flags) == {"discuss_unclear", "safe_stop"}


def test_all_days_and_outputs(tmp_path):
    entries, skipped = dr.read_entries(_log(tmp_path))
    reviews = dr.build_reviews(entries)
    assert len(reviews) == 5
    md = dr.to_markdown(reviews, None, skipped)
    assert "疑似分錯清單" in md and "EV-1" in md and "1 行無法解析" in md
    csv_text = dr.to_csv(reviews)
    assert csv_text.startswith("﻿") and "人工標註" in csv_text.splitlines()[0]


def test_cli_writes_files_and_missing_log(tmp_path, capsys):
    p = _log(tmp_path)
    out = tmp_path / "out"
    assert dr.main(["--audit", str(p), "--date", "2026-10-22", "--out", str(out)]) == 0
    assert (out / "review-2026-10-22.md").exists() and (out / "review-2026-10-22.csv").exists()
    assert dr.main(["--audit", str(tmp_path / "nope.jsonl")]) == 2
