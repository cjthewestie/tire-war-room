"""Trace-order rules over an audit log (research report 2026-11, 建議 4 / G8). Pure functions, read-only.

The hash chain (audit.scan_chain / verify_report) says the log was not edited; these rules say the
agent did things in an allowed ORDER. They read the audit entries as written (dicts, oldest first) and
return violations; nothing here writes, calls a tool or imports the agent, the API or an LLM client.

  R-a  every write ``tool_call`` (tool in gate.WRITE_TOOLS) comes after the gate let THAT step through:
       the latest ``gate`` entry for the same event + step id says ``allow``, or says
       ``needs_approval`` and a supervisor ``approval_decision`` (approve / modify) of that event follows
       it before the call. A write the supervisor picked as an approval option (step id
       ``<approval id>-<option id>``) is covered by that approval's approve / modify decision.
  R-c  no write ``tool_call`` of an event after that event's transition to ``safe_stop``. A rollback
       notice a supervisor's approved rollback sends later (gate.gated_call, payload ``by``) is not the
       event's own write and is left to R-a.
  R-d  after a VERIFY failure of an event (a ``verifier`` SYSTEM entry with ``ok: false``, or the
       transition verify -> replan), an R1 notify of that event needs a new transition to ``simulate``
       first: a fresh plan was simulated before anything goes out automatically.
  R-e  the supervisor discussion re-plans of one event (approval cards ``superseded``) never exceed
       contracts.MAX_DISCUSS_REPLANS.

Logs written before the writes-through-gate work (commit ``afb2553`` and earlier, the Spark p.11
captures in bench/results/spark-lightning-20261021/p11/) are skipped by ``legacy_reason``: their trace
order predates these rules.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from warroom.agent.gate import WRITE_TOOLS
from warroom.contracts import MAX_DISCUSS_REPLANS

RULES: dict[str, str] = {
    "R-a": "write tool_call without a gate allow, or a gate needs_approval followed by an approval, for that step",
    "R-c": "write tool_call after the event's SAFE_STOP",
    "R-d": "R1 notify after a VERIFY failure without a new SIMULATE",
    "R-e": f"more than MAX_DISCUSS_REPLANS ({MAX_DISCUSS_REPLANS}) discussion re-plans in one event",
}

# commits whose audit format predates these rules (named in the capture's file or folder name)
LEGACY_MARKERS: tuple[str, ...] = ("afb2553",)

WRITE_TOOL_NAMES = frozenset(t.value for t in WRITE_TOOLS)
APPROVING = frozenset({"approve", "modify"})


def _p(e: dict[str, Any]) -> dict[str, Any]:
    p = e.get("payload")
    return p if isinstance(p, dict) else {}


def _kind(e: dict[str, Any]) -> str:
    return str(e.get("kind") or "")


def write_tool(e: dict[str, Any]) -> str | None:
    """The write tool a ``tool_call`` entry runs, else None (the actor is ``tool:<name>``)."""
    if _kind(e) != "tool_call":
        return None
    actor = str(e.get("actor") or "")
    name = actor.split(":", 1)[1] if actor.startswith("tool:") else str(_p(e).get("tool") or "")
    return name if name in WRITE_TOOL_NAMES else None


def legacy_reason(path: str | Path | None, entries: Iterable[dict[str, Any]] = ()) -> str | None:
    """Why this log is in a format the rules do not apply to, or None."""
    name = str(path or "")
    for marker in LEGACY_MARKERS:
        if marker in name:
            return f"legacy audit format ({marker} or earlier, before writes went through the gate's permits)"
    return None


def _v(rule: str, e: dict[str, Any], msg: str, **extra: Any) -> dict[str, Any]:
    p = _p(e)
    return {"rule": rule, "seq": e.get("seq"), "event_id": e.get("event_id"), "step_id": p.get("step_id"),
            "tool": write_tool(e) or p.get("tool"), "message": msg, **extra}


def check_rules(entries: Iterable[dict[str, Any]], *,
                max_discuss_replans: int = MAX_DISCUSS_REPLANS) -> list[dict[str, Any]]:
    """All R-a, R-c, R-d, R-e violations in ``entries`` (audit entries as dicts, oldest first)."""
    out: list[dict[str, Any]] = []
    gates: dict[tuple[str | None, str], dict[str, Any]] = {}        # (event, step) -> latest gate entry
    approvals: dict[str | None, list[tuple[int, dict[str, Any]]]] = {}   # event -> [(index, decision payload)]
    safe_stopped: dict[str | None, dict[str, Any]] = {}
    verify_failed: dict[str | None, dict[str, Any]] = {}            # event -> failure entry, until a SIMULATE
    superseded: dict[str | None, int] = {}
    for i, e in enumerate(entries):
        kind, p, ev = _kind(e), _p(e), e.get("event_id")
        if kind == "gate" and p.get("step_id") is not None:
            gates[(ev, str(p["step_id"]))] = {"i": i, "entry": e}
        elif kind == "approval_decision":
            d = str(p.get("decision") or "")
            approvals.setdefault(ev, []).append((i, p))
            if d == "superseded":
                superseded[ev] = superseded.get(ev, 0) + 1
                if superseded[ev] == max_discuss_replans + 1:
                    out.append(_v("R-e", e, f"discussion re-plan {superseded[ev]} of this event exceeds "
                                            f"MAX_DISCUSS_REPLANS ({max_discuss_replans})"))
        elif kind == "state_transition":
            to, frm = p.get("to"), p.get("from")
            if to == "safe_stop":
                safe_stopped.setdefault(ev, e)
            elif to == "simulate":
                verify_failed.pop(ev, None)
            if frm == "verify" and to == "replan":
                verify_failed.setdefault(ev, e)
        elif kind == "system" and e.get("actor") == "verifier" and p.get("ok") is False:
            verify_failed.setdefault(ev, e)

        tool = write_tool(e)
        if tool is None:
            continue
        step = p.get("step_id")
        gate = gates.get((ev, str(step))) if step is not None else None
        why = _ra_problem(i, ev, step, tool, gate, approvals.get(ev, []))
        if why:
            out.append(_v("R-a", e, why))
        stop = safe_stopped.get(ev)
        if stop is not None and "by" not in p:
            out.append(_v("R-c", e, f"write after this event's SAFE_STOP (seq {stop.get('seq')})"))
        failed = verify_failed.get(ev)
        if failed is not None and tool == "notify_teams" and gate is not None and \
                _decision(gate["entry"]).get("risk") == "R1":
            out.append(_v("R-d", e, f"R1 notify after the VERIFY failure at seq {failed.get('seq')} "
                                    "with no new SIMULATE in between"))
    return out


def _decision(gate_entry: dict[str, Any]) -> dict[str, Any]:
    d = _p(gate_entry).get("decision")
    return d if isinstance(d, dict) else {}


def _approved_after(after_i: int, before_i: int, decisions: list[tuple[int, dict[str, Any]]]) -> bool:
    return any(after_i < j < before_i and str(d.get("decision")) in APPROVING for j, d in decisions)


def _ra_problem(i: int, ev: str | None, step: Any, tool: str, gate: dict[str, Any] | None,
                decisions: list[tuple[int, dict[str, Any]]]) -> str | None:
    if step is None:
        return "write tool_call carries no step id, so no gate decision can cover it"
    step = str(step)
    if gate is not None:
        d = _decision(gate["entry"])
        gtool, verdict = d.get("tool"), d.get("verdict")
        if gtool is not None and str(gtool) != tool:
            return f"the gate entry for step {step} (seq {gate['entry'].get('seq')}) judged {gtool}, not {tool}"
        if verdict == "allow":
            return None
        if verdict == "needs_approval":
            if _approved_after(gate["i"], i, decisions):
                return None
            return (f"gate needs_approval for step {step} (seq {gate['entry'].get('seq')}) with no approve / modify "
                    "decision before the write")
        return f"the latest gate verdict for step {step} is {verdict!r}"
    # an approval option the supervisor picked: step id "<approval id>-<option id>"
    for j, d in decisions:
        rid = str(d.get("request_id") or "")
        if j < i and rid and step.startswith(rid + "-") and str(d.get("decision")) in APPROVING:
            opt = d.get("option_id")
            if opt is None or step[len(rid) + 1:] == str(opt):
                return None
    return f"no gate decision for step {step} before this write"


def analyze(entries: list[dict[str, Any]], *, path: str | Path | None = None,
            max_discuss_replans: int = MAX_DISCUSS_REPLANS) -> dict[str, Any]:
    """Rules report: {"skipped": reason|None, "violations": [...], "counts": {rule: n}, "writes": n}."""
    legacy = legacy_reason(path, entries)
    if legacy:
        return {"skipped": legacy, "violations": [], "counts": {r: 0 for r in RULES}, "writes": 0}
    v = check_rules(entries, max_discuss_replans=max_discuss_replans)
    counts = {r: sum(1 for x in v if x["rule"] == r) for r in RULES}
    return {"skipped": None, "violations": v, "counts": counts,
            "writes": sum(1 for e in entries if write_tool(e))}
