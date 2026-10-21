"""Read-only tool functions owned by kb, in the Registry signature fn(ctx, args) -> dict.

The tools owner registers them as-is:
    from warroom.kb.tools import sop_search_tool, query_orders_shifts_tool
    registry.register(TOOL_SPECS[ToolName.SOP_SEARCH], sop_search_tool)
    registry.register(TOOL_SPECS[ToolName.QUERY_ORDERS_SHIFTS], query_orders_shifts_tool)

ctx is a warroom.tools.ToolContext (any field may be None). The KB comes from ctx.kb, else a
process-wide default built from Settings. The orders DB comes from ctx.orders_db if that
attribute exists, else a process-wide default (Settings.orders_xlsx imported when set,
otherwise seed data anchored on the clock's date). Outputs are plain JSON (model_dump json).
"""
from __future__ import annotations

import threading
from typing import Any

from warroom.contracts import QueryOrdersShiftsArgs, SopSearchArgs
from warroom.kb.orders_db import OrdersDB
from warroom.kb.store import SOURCE_LABELS, TEAM_SOURCE, QdrantSopKB

_lock = threading.Lock()
_default_kb: QdrantSopKB | None = None
_default_db: OrdersDB | None = None


def default_kb() -> QdrantSopKB:
    global _default_kb
    with _lock:
        if _default_kb is None:
            _default_kb = QdrantSopKB.from_settings()
        return _default_kb


def default_db(clock=None) -> OrdersDB:
    """Process-wide orders DB. Seeded in memory unless Settings.orders_xlsx points at a workbook."""
    global _default_db
    with _lock:
        if _default_db is None:
            from warroom.config import get_settings
            xlsx = get_settings().orders_xlsx
            _default_db = OrdersDB(":memory:", clock=clock, xlsx=xlsx if xlsx and xlsx.exists() else None)
        elif clock is not None and _default_db.clock is None:
            _default_db.clock = clock
        return _default_db


def set_default_db(db: OrdersDB | None) -> None:
    global _default_db
    with _lock:
        _default_db = db


TEAM_HITS_K = 2


def _hit_json(h) -> dict[str, Any]:
    d = h.model_dump(mode="json")
    d["source_label"] = SOURCE_LABELS[TEAM_SOURCE if h.source == TEAM_SOURCE else "house"]
    return d


def sop_search_tool(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """{"query", "k"} -> {"query", "mode", "hits", "team_hits", "reason"}.
    hits = the top k SOPs across the house SOPs (warroom/kb/sop) and the team dataset, ranked on one score and passed
    through the KB's no-answer gate (QdrantSopKB.retrieve); each hit carries sop_id/title/source and source_label
    (工廠 SOP / 團隊資料集). No hit -> hits [] and reason 「知識庫沒有相關 SOP：…」. team_hits = the team SOPs among hits.
    A KB without retrieve() (test fakes) keeps the old shape: house hits plus TEAM_HITS_K team hits, ungated."""
    a = SopSearchArgs.model_validate(args)
    kb = getattr(ctx, "kb", None) or default_kb()
    out: dict[str, Any] = {"query": a.query, "mode": getattr(kb, "mode", "unknown")}
    retrieve = getattr(kb, "retrieve", None)
    if callable(retrieve):
        r = retrieve(a.query, k=a.k)
        out["hits"] = [_hit_json(h) for h in r.hits]
        out["team_hits"] = [h for h in out["hits"] if h["source"] == TEAM_SOURCE]
        out["reason"] = r.reason
        return out
    out["hits"] = [h.model_dump(mode="json") for h in kb.search(a.query, k=a.k)]
    search_team = getattr(kb, "search_team", None)
    if callable(search_team):
        out["team_hits"] = [h.model_dump(mode="json") for h in search_team(a.query, k=TEAM_HITS_K)]
    return out


def query_orders_shifts_tool(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """QueryOrdersShiftsArgs -> QueryOrdersShiftsData json (orders, shifts, gaps, next_jobs) plus
    gap_details (team, headcount vs required) for the phone card."""
    a = QueryOrdersShiftsArgs.model_validate(args)
    db = getattr(ctx, "orders_db", None) or default_db(getattr(ctx, "clock", None))
    data = db.query(a)
    out = data.model_dump(mode="json")
    out["gap_details"] = db.gap_details(data.shifts)
    try:                                                    # issue #95: the same today's-orders brief, from the workbook
        from warroom.tools import orders_today
        out["orders_today"] = orders_today.collect_from_db(db, db.now())
    except Exception as e:  # noqa: BLE001
        out["orders_today_error"] = f"{type(e).__name__}: {e}"[:200]
    return out
