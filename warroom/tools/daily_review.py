"""Daily classification review (issue #44): a READ-ONLY report built from the audit log.

    python -m warroom.tools.daily_review --date 2026-10-22
    python -m warroom.tools.daily_review --audit data/runtime/audit.jsonl --out review_out

For every event of the day it lists where the event came from, how it was classified
(anomaly kind, commander cause_code), how it ended, and the signals that often mean the automatic
classification was wrong. A person fills in the last three columns (分對 / 分錯 / 原因); collected over
days, those marks are the evidence for changing the classification rules.

Read-only on purpose: it never writes the audit log, never imports the agent, tools registry, api or an
LLM client, and does not change the state machine. It does not verify the hash chain either (that is
``JsonlAuditLog`` / ``verify_chain``); it only reads the lines, skipping any it cannot parse.

Suspected-misclassification flags (``疑似分錯``):
  repeat_machine   the same machine opened more than one detector event that day
  hypothesis_swap  a hypothesis was overturned, or the commander changed cause_code within one event
  supervisor_veto  the supervisor rejected a plan, or picked a different option than in an earlier round
  discuss_unclear  a discussion message was classified unclear / unsupported
  safe_stop        the event ended in a safe stop (the automatic path gave up)
  llm_fallback     the commander's model output was rejected and the rule fallback ran
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_AUDIT = Path("data") / "runtime" / "audit.jsonl"

SOURCE_BY_REASON = (          # first state transition's reason -> where the event came from
    ("detector wake-up", "偵測器"),
    ("supervisor command", "主管指令"),
    ("plant event", "工廠事件"),
    ("patrol", "巡檢"),
    ("巡檢", "巡檢"),
)
FLAG_LABELS = {
    "repeat_machine": "同一台機一天內反覆開事件",
    "hypothesis_swap": "假設被推翻或換了原因分類",
    "supervisor_veto": "主管否決或改選",
    "discuss_unclear": "討論被分成 unclear/unsupported",
    "safe_stop": "走到安全中止",
    "llm_fallback": "模型輸出被拒，改用規則備援",
}
END_LABELS = {"closed": "結案", "safe_stop": "安全中止"}


@dataclass
class EventReview:
    event_id: str
    first_ts: str = ""
    machine_id: str = ""
    source: str = "未知"
    anomaly_kinds: list[str] = field(default_factory=list)
    anomaly_count: int = 0
    cause_codes: list[str] = field(default_factory=list)
    last_state: str = ""
    replans: int = 0
    hypotheses_overturned: int = 0
    rejects: int = 0
    modifies: int = 0
    gate_blocks: int = 0
    safety_actions: int = 0
    discuss_flagged: int = 0
    fallbacks: int = 0
    flags: list[str] = field(default_factory=list)

    @property
    def ended(self) -> str:
        return END_LABELS.get(self.last_state, "中斷／未結束")


def read_entries(path: Path) -> tuple[list[dict[str, Any]], int]:
    """(entries, skipped). Lines that are not a JSON object with kind/ts are skipped and counted."""
    entries: list[dict[str, Any]] = []
    skipped = 0
    with Path(path).open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(obj, dict) or "kind" not in obj or "ts" not in obj:
                skipped += 1
                continue
            entries.append(obj)
    return entries, skipped


def _day(ts: Any) -> str:
    return str(ts)[:10]


def _source_from(reason: str) -> str:
    low = (reason or "").lower()
    for needle, label in SOURCE_BY_REASON:
        if needle in low:
            return label
    return "未知"


def build_reviews(entries: list[dict[str, Any]], date: str | None = None) -> list[EventReview]:
    """One EventReview per event that was opened on ``date`` (all days when None)."""
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in entries:
        eid = e.get("event_id")
        if eid:
            by_event[str(eid)].append(e)

    reviews: list[EventReview] = []
    for eid, items in by_event.items():
        items.sort(key=lambda x: (x.get("seq") if isinstance(x.get("seq"), int) else 0))
        opened = [x for x in items if x.get("kind") == "state_transition"
                  and isinstance(x.get("payload"), dict) and x["payload"].get("from") is None]
        anomalies = [x for x in items if x.get("kind") == "anomaly"]
        if not opened and not anomalies:
            continue                      # e.g. an "ask" conversation, not an event
        first = (opened or anomalies)[0]
        if date and _day(first.get("ts")) != date:
            continue
        r = EventReview(event_id=eid, first_ts=str(first.get("ts", "")))
        if opened:
            r.source = _source_from(str(opened[0]["payload"].get("reason", "")))
        last_option: str | None = None      # option picked in the previous approval round of this event
        for x in items:
            p = x.get("payload") if isinstance(x.get("payload"), dict) else {}
            kind, actor = x.get("kind"), x.get("actor")
            if kind == "anomaly":
                r.anomaly_count += 1
                r.machine_id = r.machine_id or str(p.get("machine_id", ""))
                k = str(p.get("kind", ""))
                if k and k not in r.anomaly_kinds:
                    r.anomaly_kinds.append(k)
            elif kind == "state_transition":
                to = str(p.get("to", ""))
                r.last_state = to or r.last_state
                if to == "replan":
                    r.replans += 1
            elif kind == "llm_call" and actor == "commander":
                code = p.get("cause_code")
                if code and (not r.cause_codes or r.cause_codes[-1] != code):
                    r.cause_codes.append(str(code))
                if p.get("fell_back_to_mock") or p.get("plan_source") == "mock_fallback":
                    r.fallbacks += 1
            elif kind == "approval_decision":
                if p.get("decision") == "reject":
                    r.rejects += 1
                elif p.get("decision") == "modify":
                    r.modifies += 1
                opt = p.get("option_id")
                if p.get("decision") in ("approve", "modify") and opt:
                    # a later round that picks a different option than the earlier round = the supervisor
                    # changed their mind (p.11: round 1 "B 從 2 線調 1 人", round 2 "A 夜班加班")
                    if last_option is not None and opt != last_option and p.get("decision") == "approve":
                        r.modifies += 1
                    last_option = str(opt)
            elif kind in ("gate", "intercepted") and (p.get("dropped") or kind == "intercepted"):
                r.gate_blocks += 1
            elif kind == "safety_action":
                r.safety_actions += 1
            elif kind == "system":
                if actor == "verifier" and p.get("ok") is False and p.get("hypothesis"):
                    r.hypotheses_overturned += 1
                reply = p.get("discuss_reply")
                if isinstance(reply, dict) and reply.get("intent") in ("unclear", "unsupported"):
                    r.discuss_flagged += 1
        if not r.machine_id:
            r.machine_id = next((str(x["payload"].get("machine_id")) for x in items
                                 if isinstance(x.get("payload"), dict) and x["payload"].get("machine_id")), "")
        reviews.append(r)

    reviews.sort(key=lambda r: r.first_ts)
    _flag(reviews)
    return reviews


def _flag(reviews: list[EventReview]) -> None:
    per_machine = Counter(r.machine_id for r in reviews if r.source == "偵測器" and r.machine_id)
    for r in reviews:
        if r.source == "偵測器" and per_machine[r.machine_id] > 1:
            r.flags.append("repeat_machine")
        if r.hypotheses_overturned or len(r.cause_codes) > 1:
            r.flags.append("hypothesis_swap")
        if r.rejects or r.modifies:
            r.flags.append("supervisor_veto")
        if r.discuss_flagged:
            r.flags.append("discuss_unclear")
        if r.last_state == "safe_stop":
            r.flags.append("safe_stop")
        if r.fallbacks:
            r.flags.append("llm_fallback")


COLUMNS = ["event_id", "時間", "機台", "來源", "異常類型", "分類(cause_code)", "結束狀態", "REPLAN次數",
           "假設被推翻", "主管否決", "主管改選", "把關攔截", "疑似分錯", "人工標註(分對/分錯)", "原因"]


def _row(r: EventReview) -> list[str]:
    return [r.event_id, r.first_ts, r.machine_id, r.source, "、".join(r.anomaly_kinds) or "-",
            " → ".join(r.cause_codes) or "-", r.ended, str(r.replans), str(r.hypotheses_overturned),
            str(r.rejects), str(r.modifies), str(r.gate_blocks),
            "；".join(FLAG_LABELS[f] for f in r.flags), "", ""]


def to_csv(reviews: list[EventReview]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    for r in reviews:
        w.writerow(_row(r))
    return "\ufeff" + buf.getvalue()      # BOM so Excel opens the Chinese headers correctly


def _cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def to_markdown(reviews: list[EventReview], date: str | None, skipped: int = 0) -> str:
    title = f"每日分類回顧 {date}" if date else "每日分類回顧（全部日期）"
    out = [f"# {title}", ""]
    flagged = [r for r in reviews if r.flags]
    ends = Counter(r.ended for r in reviews)
    out.append(f"- 事件數：{len(reviews)}（" + "、".join(f"{k} {v}" for k, v in sorted(ends.items())) + "）"
               if reviews else "- 事件數：0")
    out.append(f"- 疑似分錯：{len(flagged)} 件")
    if skipped:
        out.append(f"- 稽核紀錄有 {skipped} 行無法解析，已略過")
    out.append("")
    out.append("## 全部事件")
    out.append("")
    out.append("| " + " | ".join(COLUMNS) + " |")
    out.append("|" + "---|" * len(COLUMNS))
    for r in reviews:
        out.append("| " + " | ".join(_cell(c) for c in _row(r)) + " |")
    out.append("")
    out.append("## 疑似分錯清單")
    out.append("")
    if not flagged:
        out.append("（沒有）")
    for r in flagged:
        out.append(f"- **{r.event_id}**（{r.machine_id or '-'}，{r.source}）："
                   + "；".join(FLAG_LABELS[f] for f in r.flags))
    out.append("")
    out.append("看完後在「人工標註」填 分對／分錯，並在「原因」寫一句話；累積幾天就是改分類規則的依據。")
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m warroom.tools.daily_review",
                                 description="Read-only daily classification review from the audit log.")
    ap.add_argument("--date", help="YYYY-MM-DD (the day the event was opened); default: all days")
    ap.add_argument("--audit", type=Path, default=DEFAULT_AUDIT, help=f"audit log (default {DEFAULT_AUDIT})")
    ap.add_argument("--out", type=Path, help="write review-<date>.md and .csv here instead of printing")
    args = ap.parse_args(argv)
    if not args.audit.exists():
        print(f"找不到稽核紀錄：{args.audit}（先跑一次情境讓系統產生它）", file=sys.stderr)
        return 2
    entries, skipped = read_entries(args.audit)
    reviews = build_reviews(entries, args.date)
    md = to_markdown(reviews, args.date, skipped)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        stem = f"review-{args.date or 'all'}"
        (args.out / f"{stem}.md").write_text(md, encoding="utf-8")
        (args.out / f"{stem}.csv").write_text(to_csv(reviews), encoding="utf-8")
        print(f"已輸出 {args.out / (stem + '.md')} 和 {args.out / (stem + '.csv')}")
    else:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
