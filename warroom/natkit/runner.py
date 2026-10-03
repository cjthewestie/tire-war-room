"""Run one war-room event end to end through the real Runtime, with the state machine traced.

Used by the NAT workflow (register.py) and by tests. The plant runs one sim second per tick; the
Runtime holds sim time while the commander or solver works, so VERIFY's 15-minute wait and the
supervisor cost no wall time. The supervisor follows the page-11 script: approve the solver's plan
at 02:16:40, then at 02:31:30 reject the cross-team move and pick night overtime (option A).
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any

from warroom import factory as F
from warroom.config import Settings
from warroom.contracts import (TERMINAL_STATES, ApprovalDecision, AuditKind, DecisionKind, LLMMode,
                               ScenarioName)
from warroom.natkit.tracing import Emitter, instrument

PAGE11_SUPERVISOR = (
    ("02:16:40", "approve", None, "同意"),
    ("02:31:30", "modify", "夜班加班", "不跨組調人，改夜班加班"),
)

SCENARIO_MACHINE = {ScenarioName.PRESS7_TEMP_DROP: F.press_id(7), ScenarioName.OPEN_MILL_OVERHEAT: "A-BIGOPEN"}


async def run_event(*, emit: Emitter, scenario: ScenarioName = ScenarioName.PRESS7_TEMP_DROP,
                    llm_mode: LLMMode = LLMMode.MOCK, base_url: str | None = None, model: str | None = None,
                    api_key: str | None = None, supervisor: str = "scripted", sim_until: str = "03:30:00",
                    max_wall_s: float = 900.0, workdir: Path | None = None) -> dict[str, Any]:
    from warroom.api import Runtime

    tmp = workdir or Path(tempfile.mkdtemp(prefix="nat-warroom-"))
    kw: dict[str, Any] = {"llm_mode": llm_mode, "llm_fallback_to_mock": llm_mode is LLMMode.MOCK,
                          "audit_path": tmp / "audit.jsonl", "schedule_dir": tmp / "sched",
                          "qdrant_path": tmp / "qdrant", "scenario": scenario.value}
    kw |= {k: v for k, v in (("base_url", base_url), ("model", model), ("api_key", api_key)) if v}
    rt = Runtime(Settings(**kw), allow_fallback=llm_mode is LLMMode.MOCK, run_loop=False)
    machine = SCENARIO_MACHINE.get(scenario)

    def at(hms: str):
        h, m, s = map(int, hms.split(":"))
        return rt.now().replace(hour=h, minute=m, second=s, microsecond=0)

    script = [(at(t), k, lbl, why) for t, k, lbl, why in PAGE11_SUPERVISOR] if supervisor == "scripted" else []
    decisions: list[dict[str, Any]] = []
    try:
        await rt.start()
        rt._ingest_kb()                                  # index the SOPs before the event, outside the clock
        instrument(rt.c.state_machine, emit)
        t0 = time.perf_counter()
        ev = None
        deadline = at(sim_until)
        while rt.now() < deadline and time.perf_counter() - t0 < max_wall_s:
            await rt.tick(1.0, publish=False, hold_compute=False)
            await rt._hold_for_compute()
            evs = [e for e in rt.events() if machine is None or e.machine_id == machine]
            ev = evs[0] if evs else None
            for req in rt.approvals.pending():
                if not script or rt.now() < script[0][0]:
                    continue
                _, kind, label, reason = script.pop(0)
                opt = (next((o for o in req.options if o.label == label), None) if label else
                       next((o for o in req.options if o.recommended), req.options[-1] if req.options else None))
                rt.approvals.decide(ApprovalDecision(request_id=req.id, decision=DecisionKind(kind),
                                                     option_id=opt.id if opt else None, reason=reason, via="phone"))
                decisions.append({"at": rt.now().strftime("%H:%M:%S"), "decision": kind,
                                  "option": opt.label if opt else None})
            if ev is not None and ev.state in TERMINAL_STATES:
                break
        if ev is None:
            return {"final_state": None, "closed": False, "error": "no event was raised",
                    "sim_end": rt.now().isoformat()}
        entries = [e for e in rt.audit.tail(0, limit=0) if e.event_id == ev.id]
        llm = [e.payload for e in entries if e.kind is AuditKind.LLM_CALL]
        tools = [e for e in entries if e.kind is AuditKind.TOOL_RESULT]
        return {
            "event_id": ev.id,
            "machine_id": ev.machine_id,
            "final_state": ev.state.value,
            "closed": ev.state.value == "closed",
            "replans": ev.replans,
            "hypotheses": [{"id": h.id, "cause": h.cause_code, "status": h.status.value} for h in ev.hypotheses],
            "llm_calls": len(llm),
            "llm_s": round(sum((p.get("total_ms") or 0) for p in llm) / 1000, 3),
            "tool_calls": len(tools),
            "tools_s": round(sum((e.payload.get("duration_ms") or 0) for e in tools) / 1000, 3),
            "decisions": decisions,
            "timeline": [t.text for t in ev.timeline],
            "audit_chain_ok": rt.audit.verify_chain(),
            "wall_s": round(time.perf_counter() - t0, 3),
        }
    finally:
        await rt.stop()
