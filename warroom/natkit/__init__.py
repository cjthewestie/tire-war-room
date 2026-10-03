"""NeMo Agent Toolkit (NAT) integration (proposal pages 5, 9, 10, 12).

- tracing: wraps the state machine's tool registry and commander so every tool call and every
  commander (LLM) call is reported as a NAT intermediate step (TOOL_START/END, LLM_START/END with
  token usage). The NAT profiler builds its latency and token reports from these steps.
- runner: runs one event through the real war-room Runtime (sim, detector, safety core, tools,
  CP-SAT, Qdrant SOP KB, gate, approvals, audit, state machine) with a scripted supervisor.
- register: the NAT plugin (entry point "nat.components" in pyproject.toml). It registers the
  `tire_war_room_event` workflow, so `nat run` / `nat eval` drive the event state machine.

NAT is not a base dependency (its profiler pins pandas<3 and rich<15). Only register.py imports it;
tracing and runner work without it. Run NAT with deploy/nat/run.sh.
"""
