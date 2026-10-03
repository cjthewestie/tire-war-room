"""In-process async pub/sub. Implemented (architect-owned).

Producers (sim loop, detector, safety, state machine, approvals) call bus.publish(type, data);
the API relays every message to WebSocket clients. Safe to call publish() from sync code
running inside the event loop; slow subscribers drop oldest messages instead of blocking."""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, AsyncIterator

from pydantic import BaseModel

from warroom.contracts import WsMessage, WsType
from warroom.factory import TZ


class InProcessBus:
    def __init__(self, maxsize: int = 1000, clock=None):
        self._subs: set[asyncio.Queue[WsMessage]] = set()
        self._maxsize = maxsize
        self._clock = clock
        self.history: list[WsMessage] = []    # last N messages, for tests and late joiners
        self._hist_max = 500

    def publish(self, type: WsType, data: BaseModel | dict[str, Any]) -> WsMessage:
        payload = data.model_dump(mode="json") if isinstance(data, BaseModel) else data
        ts = self._clock.now() if self._clock else datetime.now(TZ)
        msg = WsMessage(type=type, ts=ts, data=payload)
        self.history.append(msg)
        if len(self.history) > self._hist_max:
            del self.history[: len(self.history) - self._hist_max]
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)
        return msg

    async def subscribe(self) -> AsyncIterator[WsMessage]:
        q: asyncio.Queue[WsMessage] = asyncio.Queue(self._maxsize)
        self._subs.add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subs.discard(q)
