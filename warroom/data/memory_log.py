"""Tiered plant memory (CJ, 2026-10-22: 「加每日摘要」「那越來越多的記憶每天的記憶要怎麼管理 會壓縮成摘要嗎還是有什麼方式」
「可以試著套用claude mem的方法嗎」). Everything here is DERIVED from the audit log file, which is never deleted or
compressed: the summaries and the observations can be rebuilt from it at any time (scripts/rebuild_memory.py).

- Day keys: a factory date, or a date plus a 一日循環 cycle (循環 n): the day loop replays the same date, so each cycle
  is its own day. Rows are tagged with the key they belong to while the file is scanned (`scan`).
- Daily summary: code builds the facts (tasks by outcome; each event with its machine, hypotheses confirmed / rejected,
  card versions and decisions, outcome; open items carried over; Safety Core actions; orders shipped / late; one line
  per machine). The plain sentences come from one model call (warroom/api/memory_desk.py) or, without one, from code.
- Roll-up: 7 dailies of an ISO week (or a week that has ended) -> a weekly file; the weeks of a month -> a monthly file.
- Observations (claude-mem style): every meaningful audit row becomes a typed, one-line observation (id OB-<seq>, the
  audit seq, so a rebuild gives the same ids), stored in SQLite; the overview chat gets today's as an index.
- MemoryIndex: summaries and observations, searchable with a no-answer gate (vector + bigram when the knowledge base's
  embedder is loaded, bigram alone otherwise).

Read-only towards the plant: nothing here writes PLANT.md, the machine notes, or the audit."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

from warroom import factory as F

MAX_DAILY_SENTENCES = 8
MAX_WEEKLY_SENTENCES = 12
WEEK_DAYS = 7
MACHINE_LINE_CHARS = 160
DAILY_LINES_FOR_COMMANDER = 3          # the last 3 daily summaries' line for the machine
MIN_SCORE = 0.34                       # memory_search: below this nothing counts as found (the no-answer gate)
INDEX_MAX_LINES = 40                   # the overview chat's 今日紀錄索引
INDEX_MAX_CHARS = 1500
ICON = {"order": "📦", "event": "🛠", "approve": "✅", "reject": "❌", "safety": "⛔", "task": "👷", "ship": "🚚",
        "note": "📌"}
TYPE_LABEL = {"order": "訂單", "event": "事件", "approve": "核准", "reject": "否決", "safety": "安全", "task": "任務",
              "ship": "出貨", "note": "指示"}
_FINAL_ORDER = ("結案", "安全停止", "主管中斷", "人工處理", "處理中")
_EV_MACHINE_RE = re.compile(r"(" + "|".join(re.escape(m) for m in sorted(F.MACHINE_IDS, key=len, reverse=True)) + r")$")
_HYP_RE = re.compile(r"假設\s*(H\d+)[：:]\s*(.+)")
_ORDER_RE = re.compile(r"#[A-Z]-?\d{2,4}|SO-\d{3}")


# ====================================================================================== reading the audit file
def read_rows(path: Path | None, entries: Any = None) -> list[dict[str, Any]]:
    """Every audit row as a dict, oldest first: the JSONL file when there is one, else an in-memory audit."""
    if path is not None and Path(path).exists():
        out = []
        with Path(path).open("rb") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out
    if entries is not None:
        return [e.model_dump(mode="json") for e in entries.tail(0, 0)]
    return []


def _ts(v: Any) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))


def day_name(day: str, loop: int | None) -> str:
    return f"{day}-loop{loop}" if loop else day


def day_label(day: str, loop: int | None) -> str:
    return f"{day}（循環 {loop}）" if loop else day


def scan(rows: Iterable[dict[str, Any]], state: dict[str, Any] | None = None
         ) -> Iterator[tuple[tuple[str, int | None], dict[str, Any]]]:
    """(day key, row) for each row. The key is the factory date of the row, or, inside the 一日循環, the date that cycle
    plays and its number (rows a few seconds past 24:00 before the wrap still belong to it). `state` carries the cycle
    across calls when a scan resumes mid-file."""
    st = state if state is not None else {}
    st.setdefault("loop", None)
    st.setdefault("loop_day", None)
    for r in rows:
        p = r.get("payload") or {}
        day = _ts(r["ts"]).astimezone(F.TZ).date().isoformat()
        if r.get("kind") == "system" and p.get("msg") in ("scenario loaded", "runtime start"):
            st["loop"], st["loop_day"] = (1, day) if p.get("scenario") == "day_loop" else (None, None)
        elif r.get("kind") == "system" and r.get("actor") == "day_loop" and "loop" in p:
            st["loop"], st["loop_day"] = int(p["loop"]), day
        yield ((st["loop_day"] or day) if st["loop"] else day, st["loop"]), r


def machine_label(mid: str | None) -> str:
    from warroom.agent.narrate import machine_label as ml
    return ml(mid) if mid else "全廠"


def _machine_of(eid: str | None, p: dict[str, Any]) -> str | None:
    if p.get("machine_id") in F.MACHINE_BY_ID:
        return str(p["machine_id"])
    m = _EV_MACHINE_RE.search(eid or "")
    return m.group(1) if m else None


# ====================================================================================== daily facts
def daily_facts(rows: list[dict[str, Any]], day: str, loop: int | None) -> dict[str, Any]:
    """The facts of one day, from that day's audit rows only. Code, not a model: every number here is counted."""
    from warroom.api.tasklog import TaskLog
    tl = TaskLog()
    hyp: dict[str, dict[str, dict[str, str]]] = {}            # event -> H id -> {statement, verdict}
    safety, shipped, late, completed, notes = [], [], [], [], []
    for r in rows:
        tl._take(r)
        p, eid, kind, actor = r.get("payload") or {}, r.get("event_id"), r.get("kind"), r.get("actor")
        if kind == "approval_request" and eid:
            m = _HYP_RE.match(str(p.get("reason") or ""))
            if m:
                hyp.setdefault(eid, {}).setdefault(m.group(1), {})["statement"] = m.group(2).strip()
        elif kind == "llm_call" and actor == "commander" and eid and p.get("hypothesis"):
            h = hyp.setdefault(eid, {}).setdefault(str(p["hypothesis"]), {})
            h.setdefault("statement", str(p.get("cause_code") or ""))
        elif kind == "system" and actor == "verifier" and eid and p.get("verdict"):
            hyp.setdefault(eid, {}).setdefault(str(p.get("hypothesis")), {})["verdict"] = str(p["verdict"])
        elif kind == "safety_action":
            safety.append({"machine_id": p.get("machine_id"), "action": p.get("action"),
                           "message": str(p.get("message") or "")[:60]})
        elif kind == "system" and actor == "world" and p.get("kind") == "shipment_departed":
            shipped += list(p.get("order_ids") or [])
            if p.get("on_time") is False:
                late += list(p.get("order_ids") or [])
        elif kind == "system" and actor == "world" and p.get("kind") == "order_completed":
            completed += list(p.get("order_ids") or [])
        elif kind == "system" and actor == "world" and p.get("kind") == "delivery_late":
            late += list(p.get("order_ids") or [])
        elif kind == "system" and str(actor).startswith("admin:") and p.get("admin") == "memory_note_add":
            notes.append({"machine_id": p.get("machine_id"), "text": str(p.get("text") or "")[:60]})
    word = {"confirmed": "確認", "rejected": "推翻", "inconclusive": "無法驗證"}
    events, counts, carried = [], dict.fromkeys(_FINAL_ORDER, 0), []
    for t in tl.rows():
        mid = t.get("machine_id") or _machine_of(t["id"], {})
        hs = [f"{hid} {h.get('statement') or ''}（{word.get(h.get('verdict', ''), '未驗證')}）".replace(" （", "（")
              for hid, h in sorted(hyp.get(t["id"], {}).items())]
        e = {"id": t["id"], "machine_id": mid, "title": (t.get("title") or "")[:50], "final": t["final"],
             "hypotheses": hs, "approvals": [f"v{a['version']} {a['decision']}" for a in t["approvals"]],
             "outcome": (t.get("outcome") or "")[:80] or None}
        events.append(e)
        counts[t["final"]] = counts.get(t["final"], 0) + 1
        if t["closed_at"] is None:
            carried.append(e["id"])
    machines: dict[str, list[str]] = {}
    for e in events:
        if e["machine_id"] in F.MACHINE_BY_ID:
            bit = _short(e["title"]) + ("；" + e["hypotheses"][-1] if e["hypotheses"] else "") + f"；{e['final']}"
            machines.setdefault(e["machine_id"], []).append(bit)
    for s in safety:
        if s["machine_id"] in F.MACHINE_BY_ID:
            machines.setdefault(s["machine_id"], []).append(f"安全核心 {s['message'].split('：')[0]}")
    return {"day": day, "loop": loop, "name": day_name(day, loop), "label": day_label(day, loop),
            "tasks": len(events), "by_final": {k: v for k, v in counts.items() if v},
            "events": events, "carried_over": carried, "safety": safety,
            "orders": {"shipped": sorted(set(shipped)), "late": sorted(set(late)), "completed": sorted(set(completed))},
            "notes": notes,
            "machines": {m: _cut(f"{machine_label(m)}：" + "；".join(v), MACHINE_LINE_CHARS) for m, v in sorted(machines.items())}}


def _short(title: str) -> str:
    """A title up to its first comma: 「7 號 128 → 122°C，預測穩定在 122±1°C」 -> 「7 號 128 → 122°C」 (never cut inside a number)."""
    return re.split(r"[，,；]", title or "", maxsplit=1)[0].strip()


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def facts_text(f: dict[str, Any]) -> str:
    """The facts as plain lines: what the model reads, and the source the grounding check accepts numbers from."""
    lines = [f"日期：{f['label']}",
             f"任務 {f['tasks']} 件：" + ("、".join(f"{k} {v}" for k, v in f["by_final"].items()) or "沒有")]
    for e in f["events"]:
        lines.append(f"- {e['id']}｜{machine_label(e['machine_id'])}｜{e['title']}｜"
                     + ("假設 " + "→".join(e["hypotheses"]) + "｜" if e["hypotheses"] else "")
                     + ("卡片 " + "、".join(e["approvals"]) + "｜" if e["approvals"] else "")
                     + f"{e['final']}" + (f"｜結果 {e['outcome']}" if e["outcome"] else ""))
    if f["carried_over"]:
        lines.append(f"還沒結、轉到下一天 {len(f['carried_over'])} 件：" + "、".join(f["carried_over"]))
    if f["safety"]:
        lines.append(f"安全核心動作 {len(f['safety'])} 次：" + "；".join(s["message"] for s in f["safety"][:4]))
    o = f["orders"]
    if o["shipped"] or o["completed"] or o["late"]:
        lines.append(f"訂單：完成 {len(o['completed'])} 張、出貨 {len(o['shipped'])} 張、遲交或有風險 {len(o['late'])} 張"
                     + (f"（{'、'.join(o['late'][:5])}）" if o["late"] else ""))
    if f["notes"]:
        lines.append(f"主管新增機台筆記 {len(f['notes'])} 則")
    return "\n".join(lines)


def code_sentences(f: dict[str, Any]) -> list[str]:
    """The summary without a model: the same facts, as at most 8 plain sentences."""
    out = [f"{f['label']} 共處理 {f['tasks']} 件任務" + ("：" + "、".join(f"{k} {v} 件" for k, v in f["by_final"].items())
                                                    if f["by_final"] else "") + "。"]
    for e in f["events"][:4]:
        s = f"{machine_label(e['machine_id'])}「{_short(e['title'])}」"
        if e["hypotheses"]:
            s += "，判斷 " + "→".join(e["hypotheses"])
        if e["approvals"]:
            s += "，卡片 " + "、".join(e["approvals"])
        out.append(s + f"，{e['final']}。")
    if f["safety"]:
        out.append(f"安全核心動作 {len(f['safety'])} 次。")
    o = f["orders"]
    if o["shipped"] or o["completed"] or o["late"]:
        out.append(f"訂單完成 {len(o['completed'])} 張、出貨 {len(o['shipped'])} 張、遲交或有風險 {len(o['late'])} 張。")
    if f["carried_over"]:
        out.append(f"還有 {len(f['carried_over'])} 件沒結，轉到下一天。")
    return out[:MAX_DAILY_SENTENCES]


def facts_sha(f: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(f, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.findall(r"[^。！？!?\n]+[。！？!?]?", text) if s.strip()]


_NAMED_RE = re.compile(r"\d{1,2}\s*號機|#[A-Z]-?\d{2,4}|SO-\d{3}|EV-[0-9A-Z-]+")


def check_sentences(text: str, f: dict[str, Any]) -> list[str] | None:
    """The model's summary when it passes: ≤ 8 sentences, every number found in the facts, and every machine, order or
    event it names named in the facts too (「1 號機」 in a day that never touched press 1 is made up). None = use the
    code's sentences."""
    from warroom.agent.main_agent import ungrounded
    sents = split_sentences(text)
    if not sents or len(sents) > MAX_DAILY_SENTENCES:
        return None
    src = facts_text(f)
    if ungrounded("".join(sents), src):
        return None
    flat = src.replace(" ", "")
    if any(n.replace(" ", "") not in flat for n in _NAMED_RE.findall("".join(sents))):
        return None
    return sents


SUMMARY_SYSTEM = ("你是工廠戰情室的紀錄員。把使用者給的「當天事實」寫成給主管看的每日摘要：繁體中文、最多 8 句、白話、"
                  "不用條列、不用 Markdown。只能用事實裡出現的數字和編號，不可以加推測、不可以改數字。先說處理了幾件、"
                  "結果如何，再講重要的事件（哪台機、判斷是否被推翻、主管核准哪一版、結果），最後說還沒結的與訂單出貨。"
                  "事實裡沒寫的機台、核准、結果、出貨一律不要寫；某一項沒有就不要提。")


# ====================================================================================== files
class MemoryFiles:
    """memory/daily, memory/weekly, memory/monthly under data/runtime (gitignored). Each summary is a .md for people
    and a .json beside it (facts, sentences, source) for code."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.Lock()

    def _dir(self, level: str) -> Path:
        d = self.root / level
        d.mkdir(parents=True, exist_ok=True)
        return d

    def read(self, level: str, name: str) -> dict[str, Any] | None:
        p = self.root / level / f"{name}.json"
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        except (OSError, ValueError):
            return None

    def names(self, level: str) -> list[str]:
        d = self.root / level
        return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []

    def all(self, level: str) -> list[dict[str, Any]]:
        return [x for x in (self.read(level, n) for n in self.names(level)) if x]

    def write(self, level: str, doc: dict[str, Any]) -> Path:
        with self._lock:
            d = self._dir(level)
            for ext, text in ((".json", json.dumps(doc, ensure_ascii=False, indent=1)), (".md", render_md(level, doc))):
                tmp = d / f"{doc['name']}{ext}.tmp"
                tmp.write_text(text, encoding="utf-8")
                tmp.replace(d / f"{doc['name']}{ext}")
            return d / f"{doc['name']}.md"

    # ---------------------------------------------------------------- daily
    def daily_doc(self, f: dict[str, Any], sentences: list[str], source: str, at: str) -> dict[str, Any]:
        return {"level": "daily", "name": f["name"], "day": f["day"], "loop": f["loop"], "label": f["label"],
                "sentences": sentences, "source": source, "facts": f, "facts_sha": facts_sha(f),
                "machines": f["machines"], "written_at": at}

    def recent_daily(self, n: int) -> list[dict[str, Any]]:
        docs = self.all("daily")
        docs.sort(key=lambda d: (d["day"], d.get("loop") or 0))
        return docs[-n:]

    # ---------------------------------------------------------------- roll-up
    def roll_up(self, at: str) -> list[str]:
        """Weekly files for every ISO week with 7 dailies or that has ended; monthly files for every month with a
        weekly. Deterministic from the dailies (same input, same files). Returns the names written."""
        dailies = self.all("daily")
        if not dailies:
            return []
        weeks: dict[str, list[dict[str, Any]]] = {}
        for d in dailies:
            y, w, _ = date.fromisoformat(d["day"]).isocalendar()
            weeks.setdefault(f"{y}-W{w:02d}", []).append(d)
        latest = max(weeks)
        written = []
        for wk, ds in sorted(weeks.items()):
            if len(ds) < WEEK_DAYS and wk == latest:
                continue                                   # this week is still filling
            ds.sort(key=lambda d: (d["day"], d.get("loop") or 0))
            doc = _roll("weekly", wk, ds, MAX_WEEKLY_SENTENCES, at)
            if (self.read("weekly", wk) or {}).get("sha") != doc["sha"]:
                self.write("weekly", doc)
                written.append(wk)
        months: dict[str, list[dict[str, Any]]] = {}
        for w in self.all("weekly"):
            months.setdefault(w["days"][0][:7], []).append(w)
        for mo, ws in sorted(months.items()):
            ws.sort(key=lambda w: w["name"])
            doc = _roll("monthly", mo, ws, MAX_WEEKLY_SENTENCES, at)
            if (self.read("monthly", mo) or {}).get("sha") != doc["sha"]:
                self.write("monthly", doc)
                written.append(mo)
        return written

    def machine_lines(self, mid: str, levels: tuple[str, ...] = ("daily",), n: int = DAILY_LINES_FOR_COMMANDER
                      ) -> list[dict[str, str]]:
        """That machine's line from the last n summaries of the given levels, newest first."""
        out = []
        for level in levels:
            docs = self.all(level)
            docs.sort(key=lambda d: d["name"], reverse=True)
            for d in docs:
                line = (d.get("machines") or {}).get(mid)
                if line:
                    out.append({"level": level, "name": d["name"], "label": d.get("label", d["name"]), "line": line})
                if len([x for x in out if x["level"] == level]) >= n:
                    break
        return out


def _roll(level: str, name: str, parts: list[dict[str, Any]], max_sent: int, at: str) -> dict[str, Any]:
    tasks = sum(p.get("tasks", (p.get("facts") or {}).get("tasks", 0)) for p in parts)
    sents = [f"{name} 共 {len(parts)} 份{'每日' if level == 'weekly' else '每週'}摘要、{tasks} 件任務。"]
    for p in parts:
        first = (p.get("sentences") or [""])[0]
        sents.append(f"{p.get('label', p['name'])}：{first}" if level == "weekly" else first)
    machines: dict[str, list[str]] = {}
    for p in parts:
        for m, line in (p.get("machines") or {}).items():
            machines.setdefault(m, []).append(f"{p['name']} " + line.split("：", 1)[-1])
    days = sorted({d for p in parts for d in ([p["day"]] if "day" in p else p.get("days", []))})
    doc = {"level": level, "name": name, "label": name, "days": days, "parts": [p["name"] for p in parts],
           "tasks": tasks, "sentences": sents[:max_sent],
           "machines": {m: _cut(f"{machine_label(m)}：" + "；".join(v), MACHINE_LINE_CHARS) for m, v in sorted(machines.items())}}
    doc["sha"] = hashlib.sha256(json.dumps(doc, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    doc["written_at"] = at
    return doc


def render_md(level: str, doc: dict[str, Any]) -> str:
    title = {"daily": "每日摘要", "weekly": "每週摘要", "monthly": "每月摘要"}[level]
    out = [f"# {title} {doc.get('label', doc['name'])}", "",
           (f"<!-- memory level={level} name={doc['name']} machines={','.join(doc.get('machines') or {})} "
            f"source={doc.get('source', 'code')} -->"), "", "## 摘要", "", "".join(doc.get("sentences") or []), ""]
    if level == "daily":
        out += ["## 事實（程式整理，數字以此為準）", "", facts_text(doc["facts"]), ""]
    if doc.get("machines"):
        out += ["## 各機台", ""] + [f"- {line}" for line in doc["machines"].values()] + [""]
    out.append("由稽核紀錄整理，可用 scripts/rebuild_memory.py 重建；原始稽核紀錄不刪不壓縮。")
    return "\n".join(out) + "\n"


# ====================================================================================== observations
def observe(row: dict[str, Any]) -> dict[str, Any] | None:
    """One audit row -> one typed observation, or None. Code only; no model call."""
    p, eid, kind, actor = row.get("payload") or {}, row.get("event_id"), row.get("kind"), row.get("actor")
    if eid and str(eid).startswith(("ASK-", "MAIN-")):
        return None
    mid = _machine_of(eid, p) if eid or p.get("machine_id") else None
    ml = machine_label(mid) if mid else ""
    t, title, facts = None, None, {}
    orders: list[str] = []
    if kind == "anomaly":
        t, title, facts = "event", f"{ml}異常：{p.get('message', '')}", {"message": p.get("message"), "kind": p.get("kind")}
    elif kind == "system" and isinstance(p.get("command"), str) and eid and actor != "main_agent":
        t, title, facts = "event", f"主管指令：{p['command']}", {"command": p["command"]}
        orders = _ORDER_RE.findall(p["command"])
    elif kind == "approval_request":
        v = p.get("version") or 1
        t, title = "event", f"{ml or '計劃'} 計劃 v{v} 送核准"
        facts = {"version": v, "reason": p.get("reason"), "actions": (p.get("actions") or [])[:4],
                 "delivery_impact": p.get("delivery_impact")}
        orders = _ORDER_RE.findall(" ".join(p.get("actions") or []))
    elif kind == "approval_decision" and p.get("decision") in ("approve", "modify", "reject"):
        ok = p["decision"] != "reject"
        t = "approve" if ok else "reject"
        title = f"{ml}計劃{'核准' if ok else '否決'}" + (f"（方案 {p['option_id']}）" if p.get("option_id") else "")
        facts = {"decision": p["decision"], "option": p.get("option_id"), "reason": p.get("reason") or None,
                 "request_id": p.get("request_id"), "by": actor}
    elif kind == "system" and actor == "verifier" and p.get("verdict") in ("confirmed", "rejected"):
        t = "event"
        title = f"{ml}{p.get('hypothesis')} {'確認' if p['verdict'] == 'confirmed' else '被推翻'}"
        facts = {"hypothesis": p.get("hypothesis"), "verdict": p["verdict"], "observed": p.get("observed")}
    elif kind == "state_transition" and p.get("to") in ("closed", "safe_stop"):
        t, title = "event", f"{ml or '任務'}{'結案' if p['to'] == 'closed' else '安全停止'}"
        facts = {"state": p["to"], "reason": p.get("reason")}
    elif kind == "safety_action":
        t, title, facts = "safety", str(p.get("message") or f"{ml}安全核心動作"), {
            "action": p.get("action"), "rule": p.get("rule_id"), "value": p.get("value")}
    elif kind == "system" and actor == "supervisor" and "release" in str(p.get("msg") or p.get("action") or ""):
        t, title, facts = "safety", f"{ml}解除安全核心停機", {"msg": p.get("msg")}
    elif kind == "task" and actor == "world" and p.get("status") == "完成":
        t, title = "task", f"任務完成：{p.get('title', '')}"
        facts = {"task_id": p.get("task_id"), "worker": p.get("worker"), "kind": p.get("kind"), "where": p.get("where")}
    elif kind == "system" and actor == "world" and p.get("kind") in ("order_completed", "order_new", "order_rush",
                                                                     "delivery_late", "shipment_departed"):
        k = p["kind"]
        t = "ship" if k == "shipment_departed" else "order"
        title = str(p.get("msg") or k)
        orders = list(p.get("order_ids") or [])
        facts = {"kind": k, "qty": p.get("qty"), "on_time": p.get("on_time")}
    elif kind == "system" and actor == "agent_nemo" and p.get("msg") == "pre-plan":
        t, title = "order", f"預先規劃：{p.get('order_id')} 有交期風險"
        orders = [str(p.get("order_id"))]
        facts = {"order_id": p.get("order_id")}
    elif kind == "system" and str(actor).startswith("admin:") and p.get("admin") == "memory_note_add":
        t, title = "note", f"{machine_label(p.get('machine_id'))}筆記：{p.get('text', '')}"
        mid = p.get("machine_id")
        facts = {"note_id": p.get("note_id"), "text": p.get("text")}
    elif kind == "system" and actor == "main_agent" and p.get("pinned"):
        t, title, facts = "note", f"主管指示：{p['pinned']}", {"pinned": p["pinned"]}
    if t is None:
        return None
    return {"id": f"OB-{int(row['seq']):04d}", "seq": int(row["seq"]), "ts": row["ts"], "type": t,
            "title": _cut(" ".join(str(title).split()), 40), "machines": [mid] if mid else [],
            "orders": orders[:5], "ref": eid or (orders[0] if orders else None),
            "facts": {k: v for k, v in facts.items() if v not in (None, "", [])}}


class ObservationStore:
    """Observations in SQLite (data/runtime/observations.sqlite, gitignored). Idempotent: ids are audit seqs."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS obs (id TEXT PRIMARY KEY, seq INTEGER, ts TEXT, day TEXT, "
                       "type TEXT, title TEXT, machines TEXT, orders TEXT, ref TEXT, facts TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS obs_day ON obs(day, seq)")

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def last_seq(self) -> int:
        with self._lock, self._db() as db:
            return int(db.execute("SELECT COALESCE(MAX(seq), 0) FROM obs").fetchone()[0])

    def add(self, items: list[tuple[str, dict[str, Any]]]) -> int:
        """(day name, observation) pairs; replaces rows with the same id."""
        if not items:
            return 0
        with self._lock, self._db() as db:
            db.executemany("INSERT OR REPLACE INTO obs VALUES (?,?,?,?,?,?,?,?,?,?)",
                           [(o["id"], o["seq"], o["ts"], day, o["type"], o["title"], json.dumps(o["machines"]),
                             json.dumps(o["orders"], ensure_ascii=False), o["ref"],
                             json.dumps(o["facts"], ensure_ascii=False)) for day, o in items])
        return len(items)

    def clear(self) -> None:
        with self._lock, self._db() as db:
            db.execute("DELETE FROM obs")

    @staticmethod
    def _row(r: tuple) -> dict[str, Any]:
        return {"id": r[0], "seq": r[1], "ts": r[2], "day": r[3], "type": r[4], "title": r[5],
                "machines": json.loads(r[6]), "orders": json.loads(r[7]), "ref": r[8], "facts": json.loads(r[9])}

    def day(self, day: str) -> list[dict[str, Any]]:
        with self._lock, self._db() as db:
            return [self._row(r) for r in db.execute("SELECT * FROM obs WHERE day=? ORDER BY seq", (day,))]

    def get(self, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        with self._lock, self._db() as db:
            q = f"SELECT * FROM obs WHERE id IN ({','.join('?' * len(ids))}) ORDER BY seq"
            return [self._row(r) for r in db.execute(q, ids)]

    def all(self) -> list[dict[str, Any]]:
        with self._lock, self._db() as db:
            return [self._row(r) for r in db.execute("SELECT * FROM obs ORDER BY seq")]

    def days(self) -> list[str]:
        with self._lock, self._db() as db:
            return [r[0] for r in db.execute("SELECT day FROM obs GROUP BY day ORDER BY MAX(seq)")]


def index_line(o: dict[str, Any]) -> str:
    return f"{o['id']} · {_ts(o['ts']).astimezone(F.TZ):%H:%M} · {ICON[o['type']]} · {o['title']}"


def day_index(obs: list[dict[str, Any]], max_lines: int = INDEX_MAX_LINES, max_chars: int = INDEX_MAX_CHARS) -> str:
    """今日紀錄索引: one line per observation, newest first, grouped as 訂單 / 任務 / 其他 when long, capped."""
    obs = sorted(obs, key=lambda o: o["seq"], reverse=True)
    if len(obs) > 12:
        groups = [("訂單", [o for o in obs if o["type"] in ("order", "ship")]),
                  ("任務", [o for o in obs if o["type"] in ("event", "approve", "reject", "task")]),
                  ("其他", [o for o in obs if o["type"] in ("safety", "note")])]
    else:
        groups = [("", obs)]
    lines, n = [], 0
    for name, items in groups:
        if not items:
            continue
        if name:
            lines.append(f"【{name}】")
        for o in items:
            line = index_line(o)
            if n >= max_lines or sum(len(x) + 1 for x in lines) + len(line) > max_chars:
                lines.append(f"（還有 {len(obs) - n} 筆較早的，用 memory_search 查）")
                return "\n".join(lines)
            lines.append(line)
            n += 1
    return "\n".join(lines)


# ====================================================================================== search
def _bigram_score(q: str, text: str) -> float:
    from warroom.kb.store import lexical_score
    return lexical_score(q, text)


class MemoryIndex:
    """Summaries and observations, searchable. `kb` (the SOP knowledge base) lends its Qdrant client and embedder when
    it runs in vector mode; the `memory` collection is then rebuilt from the docs whenever they change. Otherwise the
    search is bigram only. Either way a weak best match answers nothing (MIN_SCORE)."""

    COLLECTION = "memory"

    def __init__(self, kb: Any = None):
        self.kb = kb
        self._docs: list[dict[str, Any]] = []
        self._sha = ""
        self._lock = threading.Lock()
        self.mode = "lexical"

    def set_docs(self, docs: list[dict[str, Any]]) -> None:
        sha = hashlib.sha256(json.dumps([d["id"] for d in docs] + [d["text"] for d in docs], ensure_ascii=False)
                             .encode()).hexdigest()
        with self._lock:
            if sha == self._sha:
                return
            self._docs, self._sha = docs, sha
        self._vectors()

    def _vectors(self) -> None:
        kb = self.kb
        if kb is None or getattr(kb, "mode", None) != "vector" or not self._docs:
            self.mode = "lexical"
            return
        try:
            import uuid

            from qdrant_client import models

            from warroom.kb.store import _get_client
            client = _get_client(kb.path, kb.url)
            vecs = kb._embed([d["text"] for d in self._docs])
            if client.collection_exists(self.COLLECTION):
                client.delete_collection(self.COLLECTION)
            client.create_collection(self.COLLECTION, vectors_config=models.VectorParams(
                size=len(vecs[0]), distance=models.Distance.COSINE))
            client.upsert(self.COLLECTION, points=[
                models.PointStruct(id=str(uuid.uuid5(uuid.NAMESPACE_URL, d["id"])), vector=v,
                                   payload={k: d[k] for k in ("id", "level", "date", "machines")})
                for d, v in zip(self._docs, vecs, strict=True)])
            self.mode = "vector"
        except Exception:  # noqa: BLE001 - search falls back to bigrams
            self.mode = "lexical"

    def search(self, q: str, k: int = 5, level: str | None = None, day: str | None = None,
               type_: str | None = None) -> dict[str, Any]:
        q = (q or "").strip()
        docs = [d for d in self._docs if (not level or d["level"] == level) and (not day or str(d["date"]).startswith(day))
                and (not type_ or d.get("type") == type_)]
        if not q or not docs:
            return {"found": False, "hits": [], "note": "記憶裡找不到相關內容"}
        cos: dict[str, float] = {}
        if self.mode == "vector":
            try:
                from warroom.kb.store import _get_client
                res = _get_client(self.kb.path, self.kb.url).query_points(
                    self.COLLECTION, query=self.kb._embed([q])[0], limit=max(20, k * 4), with_payload=True)
                cos = {p.payload["id"]: float(p.score) for p in res.points}
            except Exception:  # noqa: BLE001
                cos = {}
        scored = []
        for d in docs:
            lex = _bigram_score(q, d["text"])
            s = 0.5 * cos.get(d["id"], 0.0) + 0.5 * lex if cos else lex
            scored.append((round(s, 3), d))
        scored.sort(key=lambda x: -x[0])
        best = [(s, d) for s, d in scored[:k] if s >= MIN_SCORE]
        if not best:
            return {"found": False, "hits": [], "best_score": scored[0][0] if scored else 0.0,
                    "note": "記憶裡找不到相關內容（分數太低，不算查到）"}
        return {"found": True, "hits": [{"id": d["id"], "level": d["level"], "date": d["date"], "score": s,
                                         "text": _cut(d["text"], 220)} for s, d in best]}


def summary_docs(files: MemoryFiles) -> list[dict[str, Any]]:
    docs = []
    for level in ("daily", "weekly", "monthly"):
        for d in files.all(level):
            text = f"{d.get('label', d['name'])} " + "".join(d.get("sentences") or []) + " " + " ".join((d.get("machines") or {}).values())
            docs.append({"id": f"{level}:{d['name']}", "level": level, "date": d.get("day") or d["name"],
                         "machines": list(d.get("machines") or {}), "text": text})
    return docs


def observation_docs(store: ObservationStore) -> list[dict[str, Any]]:
    return [{"id": o["id"], "level": "observation", "type": o["type"], "date": o["day"], "machines": o["machines"],
             "text": f"{o['day']} {_ts(o['ts']).astimezone(F.TZ):%H:%M} {TYPE_LABEL[o['type']]} {o['title']} "
                     + " ".join(o["orders"]) + " " + json.dumps(o["facts"], ensure_ascii=False)}
            for o in store.all()]
