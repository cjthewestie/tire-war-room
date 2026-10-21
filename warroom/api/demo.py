"""Guided demo state shared by the 3D factory and the 2D war room (CJ, 2026-10-22: 「戰情室也要幫我弄一個demo模式」).

Presentation control, not plant control: one in-memory cue pointer that every screen follows. The screen that pressed
start (the "driver") runs the cue timeline (web/src/demo/cues.ts) and posts each cue it reaches; continue / pause /
exit can come from any screen (3D, war room, phone, gamepad). The only plant effect is pause / resume of the sim clock,
the same thing POST /api/sim/control does. Every change is pushed on the WebSocket bus as {"type": "demo"}.

An approval checkpoint cannot be skipped: while waiting_for == "approval", continue is refused until an approval has
been granted since the cue started waiting (the R2 rule the demo is showing)."""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from warroom.contracts import ApprovalStatus, WsType

Waiting = Literal["continue", "approval", "event"]


class DemoCommand(BaseModel):
    action: Literal["start", "continue", "pause", "resume", "exit", "cue"]
    cue_id: str | None = Field(None, max_length=40)
    cue_index: int | None = Field(None, ge=0, le=200)
    waiting_for: Waiting | None = None
    driver: str | None = Field(None, max_length=40)
    # start: the cue ids in order, so continue can name the next cue without the server knowing the timeline
    cues: list[str] | None = Field(None, max_length=60)
    story_target_s: float | None = Field(None, ge=0, le=3600)


class DemoState:
    def __init__(self, now: Callable[[], float] = time.time):
        self._now = now
        self.running = False
        self.cues: list[str] = []
        self.cue_index = 0
        self.cue_id: str | None = None
        self.started_at: float | None = None
        self.paused = False
        self.paused_since: float | None = None
        self.paused_total = 0.0
        self.waiting_for: Waiting | None = None
        self.story_target_s: float | None = None
        self.driver: str | None = None
        self.rev = 0
        self.approved_mark = 0           # approvals granted when the approval wait began

    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.paused_since if self.paused and self.paused_since is not None else self._now()
        return max(0.0, end - self.started_at - self.paused_total)

    def view(self) -> dict[str, Any]:
        return {
            "running": self.running, "cue_id": self.cue_id, "cue_index": self.cue_index,
            "started_at": datetime.fromtimestamp(self.started_at, timezone.utc).isoformat() if self.started_at else None,
            "paused": self.paused, "waiting_for": self.waiting_for, "story_target_s": self.story_target_s,
            "elapsed_s": round(self.elapsed(), 1), "driver": self.driver, "cues": self.cues, "rev": self.rev,
        }

    def _wait(self, w: Waiting | None, approved: int) -> None:
        if w == "approval" and self.waiting_for != "approval":
            self.approved_mark = approved
        self.waiting_for = w

    def apply(self, cmd: DemoCommand, approved: int) -> None:
        """One command. `approved` = approvals granted so far (the approval gate). Raises ValueError (→ 409)."""
        a = cmd.action
        if a == "start":
            self.running, self.paused, self.paused_since, self.paused_total = True, False, None, 0.0
            self.cues = list(cmd.cues or [])
            self.cue_index = cmd.cue_index or 0
            self.cue_id = cmd.cue_id or (self.cues[self.cue_index] if self.cue_index < len(self.cues) else None)
            self.started_at = self._now()
            self.driver = cmd.driver
            self.story_target_s = cmd.story_target_s
            self.waiting_for = None
            self._wait(cmd.waiting_for, approved)
        elif not self.running:
            raise ValueError("demo is not running")
        elif a == "exit":
            self.running, self.paused, self.paused_since, self.waiting_for = False, False, None, None
        elif a == "pause":
            if not self.paused:
                self.paused, self.paused_since = True, self._now()
        elif a == "resume":
            if self.paused:
                self.paused_total += self._now() - (self.paused_since or self._now())
                self.paused, self.paused_since = False, None
        elif a == "continue":
            if self.waiting_for == "approval" and approved <= self.approved_mark:
                raise ValueError("waiting for the approval: approve on the phone or the 核准 card first")
            if self.cue_index + 1 >= len(self.cues) and self.cues:
                raise ValueError("already at the last cue")
            self.cue_index += 1
            self.cue_id = self.cues[self.cue_index] if self.cue_index < len(self.cues) else None
            self.waiting_for = None
        elif a == "cue":
            if cmd.cue_index is None and cmd.cue_id is None:
                raise ValueError("cue needs cue_index or cue_id")
            idx = cmd.cue_index if cmd.cue_index is not None else (
                self.cues.index(cmd.cue_id) if cmd.cue_id in self.cues else self.cue_index)
            if idx != self.cue_index:
                self.waiting_for = None
            self.cue_index = idx
            self.cue_id = cmd.cue_id or (self.cues[idx] if idx < len(self.cues) else self.cue_id)
            self._wait(cmd.waiting_for, approved)
        self.rev += 1


def approvals_granted(r: Any) -> int:
    try:
        return sum(1 for q in r.approvals.all() if q.status in (ApprovalStatus.APPROVED, ApprovalStatus.MODIFIED))
    except Exception:  # noqa: BLE001 - a broker without all(): no approvals counted
        return 0


def install_demo(app: FastAPI, rt: Callable[[], Any], demo: Callable[..., Any]) -> DemoState:
    """GET/POST /api/demo/state. `demo` is app.py's demo-control rule (on-site seat or supervisor PIN, demo controls on)."""
    st = DemoState()
    app.state.demo = st

    @app.get("/api/demo/state")
    async def demo_state() -> dict[str, Any]:
        return st.view()

    @app.post("/api/demo/state")
    async def demo_command(cmd: DemoCommand, _: Any = Depends(demo)) -> dict[str, Any]:
        r = rt()
        try:
            st.apply(cmd, approvals_granted(r))
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        # the one plant effect: the sim clock follows the presenter's pause (story beats freeze, nothing else moves)
        if cmd.action == "pause":
            r.pause()
            r.bus.publish(WsType.CLOCK, r.clock_info())
        elif cmd.action == "resume":
            r.play()
            r.bus.publish(WsType.CLOCK, r.clock_info())
        out = st.view()
        r.bus.publish(WsType.DEMO, out)
        return out

    return st
