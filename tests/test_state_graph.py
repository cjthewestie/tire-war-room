"""web/src/agent/stateGraph.ts mirrors the state machine in warroom/contracts.py (the Agent page diagram and the 狀態機 page)."""
import pathlib
import re

from warroom import contracts as C

TS = (pathlib.Path(__file__).resolve().parent.parent / "web" / "src" / "agent" / "stateGraph.ts").read_text(encoding="utf-8")


def test_states_match():
    m = re.search(r"export const STATES: EventState\[\] = \[(.*?)\]", TS)
    assert m
    assert re.findall(r"'(\w+)'", m.group(1)) == [s.value for s in C.EventState]


def test_transitions_match():
    block = re.search(r"ALLOWED_TRANSITIONS: Record<EventState, EventState\[\]> = \{(.*?)\n\}", TS, re.S).group(1)
    got = {k: set(re.findall(r"'(\w+)'", v)) for k, v in re.findall(r"(\w+): \[([^\]]*)\]", block)}
    want = {s.value: {t.value for t in ts} for s, ts in C.ALLOWED_TRANSITIONS.items()}
    assert got == want


def test_max_failures_match():
    assert f"MAX_CONSECUTIVE_FAILURES = {C.MAX_CONSECUTIVE_FAILURES}" in TS
