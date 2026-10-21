"""問戰情室的對話記憶 (issue #95): what one conversation was just talking about, so 「那張單呢？」 can be answered.

What is kept (and only this): the order ids and event ids most recently talked about, which single order was the
focus, and the date scope ("today" and its date). NO order data: no quantity, progress, machine or due time is ever
stored here. Every follow-up looks the order up again (query_orders_shifts), so a reschedule, an inserted order or
progress since the last answer shows up in the next one, and nothing here can go stale.

Lifetime
- in this process only: a service restart forgets every conversation (nothing is written to disk);
- a conversation is whatever `conversation_id` the page sends. The page keeps it in memory, so reloading the page
  starts a new conversation (its on-screen transcript starts over too);
- a conversation idle for TTL_S is dropped, and at most MAX_CONVERSATIONS are kept (oldest idle first);
- loading or resetting a scenario (and the demo day-loop wrapping) clears every conversation: orders and events of the
  old scenario must not be pointed at in the new one.
Conversations are isolated: the key is (who asked, conversation id), so one conversation never sees another's ids."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

TTL_S = 2 * 3600.0
MAX_CONVERSATIONS = 200
MAX_IDS = 8


@dataclass
class ConvContext:
    focus_order: str | None = None                 # the one order the last answer was about (None: a list, or none)
    recent_orders: list[str] = field(default_factory=list)    # order ids of the last answer, most recent first
    event_ids: list[str] = field(default_factory=list)        # event ids named in the conversation, most recent first
    date_scope: str | None = None                  # ISO date the last order answer was about ("today" at that time)
    updated: float = 0.0

    def empty(self) -> bool:
        return not (self.focus_order or self.recent_orders or self.event_ids)

    def as_dict(self) -> dict[str, Any]:
        return {"focus_order": self.focus_order, "recent_orders": list(self.recent_orders),
                "event_ids": list(self.event_ids), "date_scope": self.date_scope}


class AskMemory:
    def __init__(self, ttl_s: float = TTL_S, max_conversations: int = MAX_CONVERSATIONS, clock=time.monotonic):
        self.ttl_s, self.max_conversations, self._clock = ttl_s, max_conversations, clock
        self._lock = threading.Lock()
        self._conv: dict[tuple[str, str], ConvContext] = {}
        self.generation = 0                         # bumped by every reset(); visible in the audit log

    @staticmethod
    def _key(actor: str, conversation_id: str | None) -> tuple[str, str] | None:
        return (actor, conversation_id) if conversation_id else None

    def _expire(self) -> None:
        now = self._clock()
        for k in [k for k, c in self._conv.items() if now - c.updated > self.ttl_s]:
            del self._conv[k]

    def get(self, actor: str, conversation_id: str | None) -> ConvContext:
        """A copy of the conversation's context (empty for a new, unknown, expired or id-less one)."""
        key = self._key(actor, conversation_id)
        with self._lock:
            self._expire()
            c = self._conv.get(key) if key else None
            if c is None:
                return ConvContext()
            return ConvContext(c.focus_order, list(c.recent_orders), list(c.event_ids), c.date_scope, c.updated)

    def remember(self, actor: str, conversation_id: str | None, *, orders: list[str] | None = None,
                 focus: str | None = None, events: list[str] | None = None, date_scope: str | None = None) -> None:
        """Record what the last answer was about. `orders` replaces the recent order ids (an answer about one
        order sets `focus` too); event ids accumulate."""
        key = self._key(actor, conversation_id)
        if key is None:
            return
        with self._lock:
            self._expire()
            c = self._conv.get(key) or ConvContext()
            if orders is not None:
                c.recent_orders = list(dict.fromkeys(orders))[:MAX_IDS]
                c.focus_order = focus
                c.date_scope = date_scope or c.date_scope
            for e in events or []:
                c.event_ids = [e] + [x for x in c.event_ids if x != e]
            c.event_ids = c.event_ids[:MAX_IDS]
            c.updated = self._clock()
            self._conv[key] = c
            if len(self._conv) > self.max_conversations:
                for k, _ in sorted(self._conv.items(), key=lambda kv: kv[1].updated)[:len(self._conv) - self.max_conversations]:
                    del self._conv[k]

    def reset(self) -> int:
        """Forget every conversation (scenario load / reset / day wrap). Returns how many were dropped."""
        with self._lock:
            n = len(self._conv)
            self._conv.clear()
            self.generation += 1
            return n

    def __len__(self) -> int:
        with self._lock:
            return len(self._conv)
