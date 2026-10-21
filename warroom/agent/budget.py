"""Model usage budget and loop guard (治理層：預算上限、迴圈上限). CJ, 2026-10-22: 「補治理」.

Budget: every model call is metered (tokens in/out from the response's usage, else an approx_tokens() estimate; wall
seconds), by kind and by event / conversation. Caps live in Settings (env-overridable):

  BUDGET_DAILY_TOKENS          3,000,000   all model tokens today (real day, Asia/Taipei)
  BUDGET_DAILY_MODEL_S         7,200       summed model wall seconds today
  BUDGET_EVENT_TOKENS          60,000      one event's commander calls
  BUDGET_CONVERSATION_TOKENS   500,000     one Agent Nemo conversation

Why these numbers (bench/results/spark-lightning-20261021, DGX Spark, Lightning): a commander call is 5.7–6.8k tokens
in + 0.6–1.3k out and 8–11 s; p.11 (plan + 2 replans) is ~23k tokens, so 60k leaves room for the plan, max_replans and
discussion replans (~7 calls) and only stops a runaway replan loop. A chat turn is 3.2k–23.7k tokens (mean 10.6k, 15
turns; 13.7k on the Mac preview 10-22, 10 turns), so 500k is ~36 turns at the Mac rate or ~21 worst-case turns in one
conversation. 3M a day is ~220 Mac-rate chat turns plus ~100 three-call incidents; 7,200 s is two hours of model time
(Mac calls are slower, so seconds bite first there). A normal demo day uses a fraction of each (Mac preview 10-22:
3 件同時異常 + 10 chat turns = 159k tokens, 104 s: 5.3 % of the day).

Degradation as the daily share (max of tokens and seconds) rises:
  ≥ 80 %  casual (small talk / general) and summary calls → the code reply / code summary
  ≥ 90 %  preplan → paused (no new pre-plans; an open pre-plan's commander call gets the rule plan)
  ≥ 100 % chat / discuss / qa / dispatch → rule mode
  never   incident and supervisor task commander calls: always sent; over the cap only logs a warning (safety > cost)
  per event: over BUDGET_EVENT_TOKENS no further REPLAN model call (the commander's own fallback: the rule plan, or
             SAFE_STOP under --strict, reason 「模型用量超過單一事件上限」). The first PLAN is never cut.
  per conversation: over BUDGET_CONVERSATION_TOKENS that conversation's chat goes to rule mode.

Every cap crossing writes one SYSTEM audit row (actor "budget", payload {"cap": ...}); the audit row is the WS event
the page shows as an alert. Each metered call writes a SYSTEM row {"meter": {...}} carrying the REAL date, so a restart
recomputes today's counters from the audit file (the audit's own ts is factory time, and the commander's LLM_CALL rows
are the only ones carrying tokens). Usage resets at the real-day boundary; the 一日循環 day-loop wrap does NOT reset it.

LoopGuard (Agent Nemo's tool loop): per turn, whether each tool result added new information. Rule A: a tool called a
third time in a turn after its last two calls added nothing → not run, no longer offered. Rule B: two rounds in a row
where every call added nothing → the next round is the final text-only one. Stops are audited (actor "loop_guard").
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from warroom.contracts import AuditKind
from warroom.factory import TZ

log = logging.getLogger(__name__)

EVENT_REASON = "模型用量超過單一事件上限"
# kind -> degradation class
KINDS: dict[str, str] = {"incident": "never", "task": "never", "preplan": "preplan", "casual": "casual",
                         "summary": "casual", "chat": "chat", "discuss": "chat", "qa": "chat", "dispatch": "chat"}
LEVELS = (("casual_code", 0.8, "模型用量達今日上限 80%：閒聊與摘要改用程式回覆"),
          ("preplan_paused", 0.9, "模型用量達今日上限 90%：暫停預先規劃"),
          ("chat_rules", 1.0, "模型用量達今日上限：對話改用規則模式；事件規劃照常呼叫模型"))
BLOCK_AT = {"casual": 0.8, "preplan": 0.9, "chat": 1.0}


class BudgetExceeded(RuntimeError):
    """A model call refused by the budget; callers' existing fallbacks (rule plan / rule reply) take over."""


def _today() -> str:
    return datetime.now(TZ).date().isoformat()


def _estimate(messages: Any) -> int:
    from warroom.agent.commander import approx_tokens
    if isinstance(messages, str):
        return approx_tokens(messages)
    text = " ".join(str(m.get("content") or "") for m in messages or [] if isinstance(m, dict))
    return approx_tokens(text)


class Budget:
    """`audit` / `bus` are callables returning the current ones (the Runtime swaps components on a scenario load);
    `is_preplan(event_id)` says whether an event is a pre-plan. `now` returns the real date (tests move it)."""

    def __init__(self, settings: Any = None, audit: Callable[[], Any] | None = None,
                 is_preplan: Callable[[str], bool] | None = None, today: Callable[[], str] = _today):
        from warroom.config import Settings
        s = settings or Settings()
        self.daily_tokens = int(getattr(s, "budget_daily_tokens", 3_000_000))
        self.daily_seconds = float(getattr(s, "budget_daily_model_s", 7200.0))
        self.event_tokens = int(getattr(s, "budget_event_tokens", 60_000))
        self.conversation_tokens = int(getattr(s, "budget_conversation_tokens", 500_000))
        self._audit = audit or (lambda: None)
        self.is_preplan = is_preplan or (lambda _eid: False)
        self._today = today
        self._reset(today())

    def _reset(self, day: str) -> None:
        self.day = day
        self.by_kind: dict[str, dict[str, float]] = defaultdict(lambda: {"calls": 0, "tokens_in": 0, "tokens_out": 0,
                                                                          "seconds": 0.0, "estimated": 0})
        self.by_event: dict[str, int] = defaultdict(int)
        self.by_conversation: dict[str, int] = defaultdict(int)
        self.crossed: set[str] = set()       # caps already announced today ("chat_rules", "event:EV-1", ...)

    def _roll(self) -> None:
        d = self._today()
        if d != self.day:
            self._reset(d)

    # ------------------------------------------------------------------ numbers
    @property
    def tokens(self) -> int:
        return int(sum(v["tokens_in"] + v["tokens_out"] for v in self.by_kind.values()))

    @property
    def seconds(self) -> float:
        return float(sum(v["seconds"] for v in self.by_kind.values()))

    def share(self) -> float:
        """Today's use as a share of the daily caps (the larger of tokens and seconds)."""
        t = self.tokens / self.daily_tokens if self.daily_tokens > 0 else 0.0
        s = self.seconds / self.daily_seconds if self.daily_seconds > 0 else 0.0
        return max(t, s)

    def level(self) -> str:
        sh, out = self.share(), "normal"
        for name, at, _text in LEVELS:
            if sh >= at:
                out = name
        return out

    # ------------------------------------------------------------------ checks
    def blocked(self, kind: str, *, event_id: str | None = None, conversation: str | None = None,
                phase: str | None = None) -> str | None:
        """Why this call must not go to the model now, or None. incident / task commander calls are never blocked by
        the daily cap; a REPLAN over the per-event cap is (any kind)."""
        self._roll()
        cls = KINDS.get(kind, "chat")
        if phase == "plan" and event_id:
            self._start_event(event_id)
        if phase == "replan" and event_id and self.by_event.get(event_id, 0) >= self.event_tokens:
            self._cross(f"event:{event_id}", EVENT_REASON, event_id=event_id, used=self.by_event[event_id],
                        limit=self.event_tokens)
            return EVENT_REASON
        if cls == "never":
            if self.share() >= 1.0:
                self._cross(f"incident_over:{event_id}", "模型用量已超過今日上限，事件規劃仍照常呼叫模型（安全優先）",
                            event_id=event_id, kind=kind)
            return None
        if cls in ("chat", "casual") and conversation and self.by_conversation.get(conversation, 0) >= self.conversation_tokens:
            self._cross(f"conversation:{conversation}", "這段對話的模型用量已達上限：改用規則模式",
                        conversation=conversation, used=self.by_conversation[conversation], limit=self.conversation_tokens)
            return "這段對話的模型用量已達上限"
        if self.share() >= BLOCK_AT[cls]:
            return {"casual": "模型用量達今日上限 80%，閒聊與摘要改用程式回覆", "preplan": "模型用量達今日上限 90%，暫停預先規劃",
                    "chat": "模型用量達今日上限，對話改用規則模式"}[cls]
        return None

    def check(self, kind: str, **kw: Any) -> None:
        why = self.blocked(kind, **kw)
        if why:
            raise BudgetExceeded(why)

    def commander_kind(self, req: Any) -> str:
        eid = getattr(req, "event_id", None)
        if eid and self.is_preplan(eid):
            return "preplan"
        kind = getattr(getattr(getattr(req, "anomaly", None), "kind", None), "value", "")
        return "task" if kind == "supervisor_command" else "incident"

    # ------------------------------------------------------------------ metering
    def record(self, kind: str, tokens_in: int | None, tokens_out: int | None, seconds: float, *,
               event_id: str | None = None, conversation: str | None = None, estimated: bool = False,
               phase: str | None = None, persist: bool = True) -> dict[str, Any]:
        self._roll()
        meter = {"day": self.day, "kind": kind, "tokens_in": int(tokens_in or 0), "tokens_out": int(tokens_out or 0),
                 "seconds": round(float(seconds), 3), "event_id": event_id, "conversation": conversation,
                 "estimated": bool(estimated), **({"phase": phase} if phase else {})}
        self._add(meter)
        if persist:
            a = self._audit()
            if a is not None:
                try:
                    a.append(AuditKind.SYSTEM, "budget", {"meter": meter})
                except Exception:  # noqa: BLE001 - metering never breaks a model call
                    log.exception("budget meter row")
        self._levels()
        return meter

    def _levels(self) -> None:
        sh = self.share()
        for name, at, text in LEVELS:
            if sh >= at:
                self._cross(name, text, share=round(sh, 3), tokens=self.tokens, seconds=round(self.seconds, 1))

    def _add(self, m: dict[str, Any]) -> None:
        k = self.by_kind[m["kind"]]
        k["calls"] += 1
        k["tokens_in"] += m["tokens_in"]
        k["tokens_out"] += m["tokens_out"]
        k["seconds"] += m["seconds"]
        k["estimated"] += 1 if m.get("estimated") else 0
        used = m["tokens_in"] + m["tokens_out"]
        if m.get("event_id"):
            if m.get("phase") == "plan":
                self._start_event(m["event_id"])
            self.by_event[m["event_id"]] += used
        if m.get("conversation") is not None:
            self.by_conversation[m["conversation"]] += used

    def _start_event(self, event_id: str) -> None:
        """An event's PLAN starts its count: a reloaded scenario replays the same event ids (EV-20261003-100133-B-OPEN on
        every 3 件同時異常), and a rehearsal must not inherit the last one's use or warnings."""
        self.by_event[event_id] = 0
        self.crossed -= {f"event:{event_id}", f"incident_over:{event_id}"}

    def _cross(self, key: str, text: str, **info: Any) -> None:
        if key in self.crossed:
            return
        self.crossed.add(key)
        log.warning("budget: %s %s", text, info)
        a = self._audit()
        if a is not None:
            try:
                a.append(AuditKind.SYSTEM, "budget", {"cap": key.split(":")[0], "key": key, "day": self.day,
                                                      "text": text, **info}, event_id=info.get("event_id"))
            except Exception:  # noqa: BLE001
                log.exception("budget cap row")

    def load(self, path: Path | str | None) -> int:
        """Recompute today's counters from the audit file's budget rows (a restart keeps the budget). Returns rows read."""
        self._reset(self._today())
        p = Path(path) if path else None
        if p is None or not p.is_file():
            return 0
        n = 0
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"budget"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                pl = row.get("payload") or {}
                if row.get("actor") != "budget" or pl.get("day") not in (None, self.day):
                    continue
                m = pl.get("meter")
                if isinstance(m, dict) and m.get("day") == self.day:
                    self._add({**m, "tokens_in": int(m.get("tokens_in") or 0), "tokens_out": int(m.get("tokens_out") or 0),
                               "seconds": float(m.get("seconds") or 0)})
                    n += 1
                elif pl.get("key") and pl.get("day") == self.day:
                    self.crossed.add(str(pl["key"]))
        self._levels()             # lower caps by env after a restart: announced now, not at the next call
        return n

    # ------------------------------------------------------------------ view
    def view(self, top: int = 5) -> dict[str, Any]:
        self._roll()
        sh = self.share()
        return {
            "day": self.day, "level": self.level(), "share": round(sh, 4),
            "totals": {"tokens": self.tokens, "seconds": round(self.seconds, 1),
                       "calls": int(sum(v["calls"] for v in self.by_kind.values()))},
            "by_kind": {k: {**v, "seconds": round(v["seconds"], 1)} for k, v in sorted(self.by_kind.items())},
            "top_events": [{"event_id": k, "tokens": v, "pct": round(v / self.event_tokens * 100, 1) if self.event_tokens else None}
                           for k, v in sorted(self.by_event.items(), key=lambda x: -x[1])[:top]],
            "top_conversations": [{"conversation": k or "(預設)", "tokens": v,
                                   "pct": round(v / self.conversation_tokens * 100, 1) if self.conversation_tokens else None}
                                  for k, v in sorted(self.by_conversation.items(), key=lambda x: -x[1])[:top]],
            "caps": {"daily_tokens": self.daily_tokens, "daily_model_s": self.daily_seconds,
                     "per_event_tokens": self.event_tokens, "per_conversation_tokens": self.conversation_tokens},
            "used_pct": {"daily_tokens": round(self.tokens / self.daily_tokens * 100, 2) if self.daily_tokens else None,
                         "daily_model_s": round(self.seconds / self.daily_seconds * 100, 2) if self.daily_seconds else None},
            "degradation": {
                "casual_code": sh >= BLOCK_AT["casual"], "preplan_paused": sh >= BLOCK_AT["preplan"],
                "chat_rules": sh >= BLOCK_AT["chat"], "incidents": "always planned by the model (over the cap: warning only)",
                "events_cut": sorted(k.split(":", 1)[1] for k in self.crossed if k.startswith("event:")),
                "conversations_cut": sorted(k.split(":", 1)[1] for k in self.crossed if k.startswith("conversation:")),
            },
            "resets": "real-day boundary (Asia/Taipei); the 一日循環 loop wrap does not reset it",
        }


_BUDGET = Budget()


def get() -> Budget:
    return _BUDGET


def install(b: Budget) -> Budget:
    global _BUDGET
    _BUDGET = b
    return b


async def create(client: Any, kind: str, *, event_id: str | None = None, conversation: str | None = None,
                 **kw: Any) -> Any:
    """client.chat.completions.create(**kw), checked and metered. Raises BudgetExceeded before sending when refused;
    a failed call is metered for its wall seconds and the estimated prompt."""
    b = get()
    b.check(kind, event_id=event_id, conversation=conversation)
    t0 = time.perf_counter()
    try:
        r = await client.chat.completions.create(**kw)
    except Exception:
        b.record(kind, _estimate(kw.get("messages")), 0, time.perf_counter() - t0, event_id=event_id,
                 conversation=conversation, estimated=True)
        raise
    u = getattr(r, "usage", None)
    pin, pout = getattr(u, "prompt_tokens", None), getattr(u, "completion_tokens", None)
    est = not isinstance(pin, int) or not isinstance(pout, int)
    if not isinstance(pin, int):
        pin = _estimate(kw.get("messages"))
    if not isinstance(pout, int):
        try:
            m = r.choices[0].message
            calls = " ".join(f"{c.function.name} {c.function.arguments}" for c in (m.tool_calls or []))
            pout = _estimate(f"{m.content or ''} {calls}")
        except Exception:  # noqa: BLE001
            pout = 0
    b.record(kind, pin, pout, time.perf_counter() - t0, event_id=event_id, conversation=conversation, estimated=est)
    return r


# ====================================================================================== loop guard
LOOP_STOP = "這個工具這一輪已經查不到新東西，請用目前資料回答，或說查不到"
_EMPTY_RX = re.compile(r"^\s*(?:失敗|沒有找到|查無|找不到|沒有資料|沒有讀值|無資料|沒有這)|(?:^|[^\d])0\s*筆|共\s*0\s*")


def _empty(result: str) -> bool:
    t = (result or "").strip()
    if not t or _EMPTY_RX.search(t[:80]):
        return True
    try:
        v = json.loads(t)
    except ValueError:
        return False

    def hollow(x: Any) -> bool:
        if isinstance(x, dict):
            return all(hollow(y) for y in x.values())
        if isinstance(x, list):
            return all(hollow(y) for y in x)
        return x in (None, "", 0, False)
    return hollow(v)


class LoopGuard:
    """One per Agent Nemo turn. `allow(tool)` before running a call (False → reply LOOP_STOP, the tool is no longer
    offered); `saw(tool, result)` after; `end_round()` → True when the next round must be the final text-only one."""

    def __init__(self, turn_id: str = ""):
        self.turn_id = turn_id
        self.calls: dict[str, list[bool]] = defaultdict(list)   # tool -> added new info, per call
        self.seen: set[str] = set()
        self.stopped: set[str] = set()
        self.round: list[bool] = []
        self.dry_rounds = 0
        self.forced = False

    def allow(self, tool: str) -> bool:
        if tool in self.stopped:
            return False
        h = self.calls[tool]
        if len(h) >= 2 and not h[-1] and not h[-2]:              # Rule A: this would be the 3rd, last two added nothing
            self.stopped.add(tool)
            self.round.append(False)
            self._audit({"rule": "A", "tool": tool, "calls": len(h) + 1, "text": LOOP_STOP})
            return False
        return True

    def saw(self, tool: str, result: str) -> bool:
        h = hashlib.sha256((result or "").encode("utf-8")).hexdigest()
        new = not _empty(result) and h not in self.seen
        self.seen.add(h)
        self.calls[tool].append(new)
        self.round.append(new)
        return new

    def end_round(self) -> bool:
        if self.round:
            self.dry_rounds = 0 if any(self.round) else self.dry_rounds + 1
        self.round = []
        if self.dry_rounds >= 2 and not self.forced:           # Rule B
            self.forced = True
            self._audit({"rule": "B", "rounds": self.dry_rounds, "text": "連續兩輪查不到新東西，改成直接回答"})
        return self.forced

    def offered(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [x for x in tools if x["function"]["name"] not in self.stopped]

    def _audit(self, payload: dict[str, Any]) -> None:
        a = get()._audit()
        if a is not None:
            try:
                a.append(AuditKind.SYSTEM, "loop_guard", {"turn": self.turn_id, **payload})
            except Exception:  # noqa: BLE001
                log.exception("loop guard row")
