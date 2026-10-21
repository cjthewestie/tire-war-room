"""Rebuild the plant memory from the audit log (「接記憶」). The audit is the source of truth and is only read.

    uv run python scripts/rebuild_memory.py                 # data/runtime/audit.jsonl -> data/runtime/memory + observations
    uv run python scripts/rebuild_memory.py --audit PATH    # another audit file (its folder gets the memory)

Writes every daily summary (one per factory date, or per 一日循環 cycle), the weekly / monthly roll-ups, and the
observations table. Idempotent: running it twice gives the same files and rows. A daily summary whose facts did not
change keeps the sentences it has (a model-written summary is not replaced by the code's); a new or changed one gets
the code's sentences (no model call here)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from warroom.data import memory_log as ML


def rebuild(audit: Path, at: str | None = None) -> dict[str, int]:
    base = audit.parent
    files = ML.MemoryFiles(base / "memory")
    store = ML.ObservationStore(base / "observations.sqlite")
    rows = ML.read_rows(audit)
    by_key: dict[tuple[str, int | None], list[dict]] = {}
    obs = []
    for key, r in ML.scan(rows):
        by_key.setdefault(key, []).append(r)
        o = ML.observe(r)
        if o is not None:
            obs.append((ML.day_name(*key), o))
    written = 0
    for (day, loop), rs in sorted(by_key.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        f = ML.daily_facts(rs, day, loop)
        if not f["tasks"] and not f["safety"] and not any(f["orders"].values()):
            continue                                       # nothing happened that day: no file
        old = files.read("daily", f["name"])
        if old and old.get("facts_sha") == ML.facts_sha(f):
            continue                                       # same facts: keep the file as it is
        stamp = at or (rs[-1]["ts"] if rs else "")
        files.write("daily", files.daily_doc(f, ML.code_sentences(f), "code", stamp))
        written += 1
    rolled = files.roll_up(at or (rows[-1]["ts"] if rows else ""))
    store.clear()
    store.add(obs)
    return {"rows": len(rows), "days": len(by_key), "daily_written": written, "rolled_up": len(rolled),
            "observations": len(obs)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--audit", type=Path, default=Path(__file__).resolve().parent.parent / "data" / "runtime" / "audit.jsonl")
    a = ap.parse_args()
    if not a.audit.exists():
        sys.exit(f"no audit file at {a.audit}")
    print(rebuild(a.audit))


if __name__ == "__main__":
    main()
