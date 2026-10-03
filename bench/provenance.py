"""Provenance of every saved bench result: which bench code produced it.

save_result stamps each JSON with the git sha, whether the bench's source files had local edits, and a
fingerprint of those files' content; bench.report marks a result STALE once the files differ from the run.
Kept out of llm.py on purpose: this file is not one of the fingerprinted dependencies, so changing the
bookkeeping here does not invalidate every result.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# Which bench source files decide each result's numbers. A result is only current while these files are
# byte-identical to the run that produced it (bench.report flags it STALE otherwise).
BENCH_DIR = Path(__file__).resolve().parent
BENCH_DEPS: dict[str, tuple[str, ...]] = {
    "ttft": ("ttft.py", "llm.py", "prompts.py"),
    "concurrent": ("concurrent.py", "llm.py", "prompts.py"),
    "full_event": ("full_event.py", "llm.py", "prompts.py"),
    "accuracy": ("accuracy.py", "llm.py", "prompts.py"),
    "memory": ("memory_check.py",),                # uses llm.save_result only
    "unplug": ("unplug_test.py", "llm.py", "prompts.py", "memory_check.py"),
    "tokens_per_watt": ("tokens_per_watt.py", "llm.py", "prompts.py"),
}


def code_fingerprint(name: str) -> str | None:
    """sha256 over the bench source files `name`'s numbers depend on (content, so a dirty tree counts)."""
    import hashlib
    deps = BENCH_DEPS.get(name)
    if not deps:
        return None
    h = hashlib.sha256()
    for f in deps:
        p = BENCH_DIR / f
        h.update(f.encode() + b"\0" + (p.read_bytes() if p.exists() else b"<missing>") + b"\0")
    return h.hexdigest()[:16]


def code_stamp(name: str) -> dict[str, Any]:
    """git sha + dirty flag + bench-code fingerprint, written into every result."""
    import subprocess

    def git(*a: str) -> str | None:
        try:
            r = subprocess.run(["git", *a], cwd=BENCH_DIR, capture_output=True, text=True, timeout=5)
            return r.stdout.strip() if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None
    deps = BENCH_DEPS.get(name, ())
    dirty = git("status", "--porcelain", "--", *deps) if deps else None
    return {"git_sha": git("rev-parse", "--short=12", "HEAD"), "deps": list(deps),
            "deps_dirty": None if dirty is None else bool(dirty), "fingerprint": code_fingerprint(name)}
