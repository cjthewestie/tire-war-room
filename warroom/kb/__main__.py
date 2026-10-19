"""CLI:  uv run python -m warroom.kb <command>
  ingest                       build the Qdrant SOP index (Settings.qdrant_path / qdrant_url)
  search <query...>            top-3 SOPs with citations, then the top-2 team dataset SOPs
  list                         SOP ids and titles
  orders [--press CURE-07]     query_orders_shifts JSON (seed data, demo date 2026-10-03)
  export <out.xlsx>            write the seed DB as an Excel workbook (= import template)
  import <in.xlsx>             import a workbook and print the row counts + a query
  quiz                         quiz size and categories
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from warroom.contracts import QueryOrdersShiftsArgs


def main(argv: list[str]) -> int:
    cmd, rest = (argv[0], argv[1:]) if argv else ("help", [])
    if cmd in ("ingest", "search", "list"):
        from warroom.kb.store import QdrantSopKB
        kb = QdrantSopKB.from_settings()
        if cmd == "ingest":
            print(f"indexed {kb.ingest()} chunks from {len(kb.titles())} SOPs (mode={kb.mode}); "
                  f"team dataset {len(kb._team_chunks)} chunks from {len(kb.team_titles())} SOPs "
                  f"(mode={kb.team_mode})")
        elif cmd == "list":
            for sid, title in sorted(kb.titles().items()):
                print(sid, title)
            for sid, title in sorted(kb.team_titles().items()):
                print(sid, title, "[team_dataset]")
        else:
            q = " ".join(rest)
            for h in kb.search(q) + kb.search_team(q):
                print(f"[{h.score}] {h.sop_id} {h.title} ({h.source})\n{h.text[:200]}\n")
        return 0
    if cmd in ("orders", "export", "import"):
        from warroom.kb.orders_db import OrdersDB
        db = OrdersDB(":memory:")
        if cmd == "export":
            print(db.export_excel(Path(rest[0])))
            return 0
        if cmd == "import":
            db.clear()
            print(db.import_excel(Path(rest[0])))
        press = rest[rest.index("--press") + 1] if "--press" in rest else None
        data = db.query(QueryOrdersShiftsArgs(press_ids=[press] if press else None))
        print(json.dumps(data.model_dump(mode="json"), ensure_ascii=False, indent=1))
        return 0
    if cmd == "quiz":
        from warroom.kb.quiz import load_quiz
        q = load_quiz()
        cats: dict[str, int] = {}
        for item in q:
            cats[item["category"]] = cats.get(item["category"], 0) + 1
        print(len(q), cats)
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
