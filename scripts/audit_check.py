"""Offline audit checker (research report 2026-11, 建議 4 / G8). Read-only: it never repairs, archives or appends.

    uv run python scripts/audit_check.py data/runtime/audit.jsonl                 # key from the env or <log>.key
    uv run python scripts/audit_check.py copy.jsonl --key-file /run/credentials/warroom/audit.key
    uv run python scripts/audit_check.py bench/results/mac-p11-fix-20261022/runs/run1-audit.jsonl   # no key: links only

1. the hash chain (audit.verify_file: the same scan as JsonlAuditLog.verify_report, plus the signed head anchor
   ``<log>.head`` when present). Without a key only seq order and prev_hash links are checked (``hmac_checked``
   false). A capture that starts mid-chain (one run's rows) is checked as an excerpt.
2. the trace-order rules R-a, R-c, R-d, R-e (warroom/agent/audit_rules.py). Captures in the pre-gate format
   (afb2553 and earlier) are skipped and say so.

Prints a JSON report; exit 0 when the chain verifies and no rule is broken, 1 otherwise, 2 on a usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from warroom.agent.audit import KEY_ENV, KEY_FILE_ENV, verify_file
from warroom.agent.audit_rules import analyze


def read_key(log: Path, key_file: Path | None) -> tuple[bytes | None, str]:
    """(key, source) without creating anything: --key-file, WARROOM_AUDIT_KEY, WARROOM_AUDIT_KEY_FILE,
    then <log>.key beside the log; (None, "none") when there is no key."""
    if key_file is not None:
        return key_file.read_bytes().strip(), "key_file"
    env = os.environ.get(KEY_ENV)
    if env:
        try:
            return bytes.fromhex(env), "env"
        except ValueError:
            return env.encode("utf-8"), "env"
    kf = os.environ.get(KEY_FILE_ENV)
    if kf:
        return Path(kf).read_bytes().strip(), "file"
    beside = log.with_name(log.name + ".key")
    if beside.exists():
        return beside.read_bytes(), "beside_log"
    return None, "none"


def entries_for_rules(log: Path) -> list[dict[str, Any]]:
    """Every line that parses as JSON (rules still run on a broken chain, so the report shows both)."""
    out: list[dict[str, Any]] = []
    for line in log.read_bytes().splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line.decode("utf-8"))
        except Exception:  # noqa: BLE001 - the chain check reports the bad line
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def check(log: Path, key_file: Path | None = None, head: Path | None = None) -> dict[str, Any]:
    key, source = read_key(log, key_file)
    chain = verify_file(log, key, head_path=head)
    chain["key_source"] = source
    rules = analyze(entries_for_rules(log) if log.exists() else [], path=log)
    ok = bool(chain["ok"]) and not rules["violations"]
    return {"file": str(log), "ok": ok, "chain": chain, "rules": rules}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Verify an audit log's hash chain and trace-order rules (read-only).")
    ap.add_argument("log", type=Path, help="audit .jsonl file")
    ap.add_argument("--key-file", type=Path, default=None, help="audit HMAC key (default: env, then <log>.key)")
    ap.add_argument("--head", type=Path, default=None, help="head anchor to require (default: <log>.head if present)")
    args = ap.parse_args(argv)
    if args.key_file is not None and not args.key_file.exists():
        print(f"key file {args.key_file} not found", file=sys.stderr)
        return 2
    rep = check(args.log, args.key_file, args.head)
    print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    return 0 if rep["ok"] else 1


__all__ = ["check", "main", "read_key"]

if __name__ == "__main__":
    raise SystemExit(main())
