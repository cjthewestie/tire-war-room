"""Issue #32: the patrol. Code, not the LLM, looks over the plant and wakes the commander only when it finds
something (page 5: the LLM wakes only for an event).

CJ, 2026-10-20: 「24小時醒著沒錯但是有沒有需要去主動巡檢」.

What it watches (this build): materials. Every PATROL_EVERY_S of factory time, and at once after a stock-out,
a late or short delivery, or a stock count, it projects each material at the rate the running machines burn it.
A material that runs out within HORIZON_H, before the next purchase is due in, becomes one `material_short`
plant input; the Factory hands it to the runtime like a rush order, so the commander plans it (查原料 → 派工
盤點／催貨／調整投料, R2 approval) and people do the tasks. One finding per material until its stock is back above
the reorder point or REARM_H has passed, so the same shortfall never opens a second event.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from warroom.contracts import PlantInput

if TYPE_CHECKING:
    from warroom.sim.world import World, WorldEvent

PATROL_EVERY_S = 1800.0      # a round every 30 factory minutes
HORIZON_H = 8.0              # what runs out within this many hours is worth a supervisor's look
REARM_H = 6.0                # the same material is not raised again sooner, unless its stock recovered
WAKE_ON = frozenset({"stockout", "delivery_late", "delivery_short", "stock_check"})


class MaterialPatrol:
    def __init__(self, world: World, t0: datetime):
        self.world = world
        self._next = t0 + timedelta(seconds=PATROL_EVERY_S)
        self._due = False
        self._raised: dict[str, datetime] = {}          # material key -> when it was raised
        world.add_listener(self._on_world_event)

    def _on_world_event(self, ev: WorldEvent) -> None:
        if ev.kind in WAKE_ON:
            self._due = True

    def poll(self, now: datetime) -> list[PlantInput]:
        """Called after the world advanced. Returns what this round found (usually nothing)."""
        if not self._due and now < self._next:
            return []
        self._due = False
        self._next = now + timedelta(seconds=PATROL_EVERY_S)
        return [pin for o in self.world.material_outlook() if (pin := self._check(o, now)) is not None]

    def _check(self, o: dict[str, Any], now: datetime) -> PlantInput | None:
        key = o["key"]
        if key in self._raised:
            if o["stock"] > o["reorder_point"] or now - self._raised[key] >= timedelta(hours=REARM_H):
                del self._raised[key]
            else:
                return None
        h = o["hours_left"]
        if h is None or h > HORIZON_H:                  # nothing burns it right now, or there is time
            return None
        po = o["next_po"]
        runs_out = now + timedelta(hours=h)
        if po is not None:
            due = now if po["status"] == "卸貨中" else po["eta"] if po["status"] == "在途" else None
            if due is not None and due <= runs_out:     # the next delivery lands before it runs out
                return None
        self._raised[key] = now
        return PlantInput(kind="material_short", text=_text(o, po, runs_out), material_key=key, note=o["name"])


def _text(o: dict[str, Any], po: dict[str, Any] | None, runs_out: datetime) -> str:
    """巡檢：碳黑 可用 60 kg，約 6.0 小時後（20:00）用完；PO-4 預計 10/09 14:00 到，晚 18.0 小時"""
    left = "已用完" if o["hours_left"] <= 0 else f"約 {o['hours_left']:.1f} 小時後（{runs_out:%H:%M}）用完"
    if po is None:
        nxt = "目前沒有在途採購"
    elif po["status"] == "延遲":
        nxt = f"{po['id']} 已延遲，到貨時間未定"
    else:
        gap = (po["eta"] - runs_out).total_seconds() / 3600
        nxt = f"{po['id']} 預計 {po['eta']:%m/%d %H:%M} 到，晚 {gap:.1f} 小時"
    return f"巡檢：{o['name']} 可用 {o['stock']:g} {o['unit']}，{left}；{nxt}"
