# Architecture — 實心胎智慧製造戰情室 (Team Westie)

Source of truth: `docs/proposal-final.pdf` (p.N below). Contracts: `warroom/contracts.py` (+ mirror `web/src/contracts.ts`). Static factory data and every proposal number: `warroom/factory.py`. If this file and the PDF disagree, the PDF wins.

## Module map and ownership

One directory per owner. Do not edit another owner's directory; ask for a contract change instead.

| Dir | Owner | Implements (contracts Protocol) | Main classes |
|---|---|---|---|
| `warroom/contracts.py`, `factory.py`, `clock.py`, `bus.py`, `config.py` | architect | shared types, constants, SimClock, InProcessBus, Settings | done |
| `warroom/sim/` | sim | `FactorySim` | `Factory`, `get_scenario`, `ReplaySource`, `record_jsonl` |
| `warroom/detect/` | detect | `Detector` | `StreamDetector`, `validate_reading` |
| `warroom/safety/` | safety | `SafetyCore` | `RuleSafetyCore`, `default_rules` |
| `warroom/tools/` | tools | `ToolRegistry` | `Registry`, `ToolContext`, `build_registry`, `TOOL_SPECS`, `arrhenius_cure` |
| `warroom/schedule/` | schedule | `Scheduler`, `ScheduleStore` | `CpSatScheduler`, `HeuristicScheduler`, `FallbackScheduler`, `FileScheduleStore`, `back_propagate`, `capacity_report` |
| `warroom/kb/` | kb | `SopKB` | `QdrantSopKB`; corpus `warroom/kb/sop/*.md` |
| `warroom/agent/` | agent | `CommanderClient`, `ActionGate`, `AuditLog`, `ApprovalBroker`, `EventStateMachine` | `OpenAICommander`, `MockCommander`, `make_commander`, `RuleGate`, `JsonlAuditLog`, `ApprovalBrokerImpl`, `StateMachine` |
| `warroom/api/` | api | REST + `/ws` (`REST_ENDPOINTS`), `Runtime` wiring + main loop | `create_app`, `Runtime` |
| `web/` | web | dashboard `index.html` (p.2) + phone `phone.html` (p.11 card) | React + Vite + TS |
| `deploy/`, `bench/` | deploy | docker-compose for Spark (vLLM Nemotron NVFP4, Qdrant), systemd/watchdog, unplug test, latency/memory benchmarks | — |
| `tests/` | each owner adds `tests/test_<module>.py` | | |
| `legacy/` | read-only | old 1-commander+4-agent prototype (superseded), old `sop_kb.py`, `tool_call_test.py` | |

## Data flow (p.5, p.10)

```
Factory sim (28 machines, replay + fault injection)        Orders/shifts (Excel import, no MES)
   │ SensorReading per machine per second                     │
   ├──────────────► SafetyCore.check  ── SafetyAction ──► sim.apply_safety_action   (RED path, <0.2 s, no LLM)
   ▼
Detector.validate (bad data → never wakes AI) → Detector.ingest → Anomaly (≤60 s after fault)
   ▼ wake
StateMachine.handle(anomaly) ──► CommanderClient.plan (ONLY LLM, JSON = CommanderPlan schema)
   │  read/compute tools auto-run via ToolRegistry          write tools → ActionGate → (R2/R3) ApprovalBroker → phone
   ▼                                                        ▼
AuditLog (append-only, hash chain) ◄── every transition, LLM call, tool call, gate, approval, safety action
   ▼
InProcessBus ──► FastAPI /ws ──► dashboard + phone
```

`Runtime` (api owner) runs the loop: `sim.step(1)` → `safety.check` → `sim.apply_safety_action` → `detector.ingest` → `asyncio.create_task(state_machine.handle(a))` → `bus.publish(MACHINES …)`. The safety step never awaits anything.

## Event lifecycle (p.5, p.7, p.11)

States: `EventState` with `ALLOWED_TRANSITIONS`. `DETECTED → CLASSIFY → PLAN → SIMULATE → GATE → [AWAIT_APPROVAL] → EXECUTE → VERIFY → CLOSED`; `VERIFY`/`SIMULATE`/`GATE`/`AWAIT_APPROVAL` can go to `REPLAN → SIMULATE` (swap hypothesis); any state → `SAFE_STOP`.

- **CLASSIFY**: read 30-min sensors, `predict_anomaly`, `sop_search`. **PLAN**: commander returns `CommanderPlan` — goal, `Hypothesis` with `Expectation` (e.g. CURE-07 temp ≥121 within 30 min), steps S1..Sn with `depends_on`; args may reference prior results as `"$S1.total_cure_h"`.
- **SIMULATE**: run read/compute steps in dependency order (S1 ∥ S2, then S3 solve). **GATE**: each write step → `GateDecision` (allow-list, state allowed, format, simulation feasible, risk R0–R3). R2/R3 → `ApprovalRequest` with options (A 夜班加班 / B 從 2 線調 1 人).
- **EXECUTE**: write tools only record intent (schedule activate, cure-time suggestion, notifications). No PLC/actuator writes exist anywhere.
- **VERIFY** is code, not LLM: compare the expectation with live readings. Miss → hypothesis REJECTED → **REPLAN** with the rejected list.
- Failure policy (`MAX_TOOL_RETRIES=1`, `MAX_CONSECUTIVE_FAILURES=2`): retry once, then SOP path; CP-SAT timeout → heuristic backup (`fell_back=True`); 2 consecutive failures → `SAFE_STOP`, previous schedule kept. Schedule versions kept; one-click rollback.
- Tools opened by state: `TOOLS_BY_STATE`. Write tools only in EXECUTE.

## Key numbers

| What | Value | Page |
|---|---|---|
| Machines / lines / processes | 28 / 4 + bond + cure / 26 | p.2, p.3 |
| Curing presses | 11, one tire each; 7 small-only, 4 shared M/L (we fix M/L = CURE-07..10) | p.3, p.11 |
| Cure time | S 24 h / M 28 h / L 34 h at 120–130 °C, 20 kgf/cm² | p.3 |
| Safety core | 煉膠 ≥120 °C stop; 已加硫 ≥110 °C stop; press <120 °C → cure hold | p.5, p.2, p.11 |
| Arrhenius | Ea 85 kJ/mol (assumed); M 128 °C, 10 h done, drops to 122 °C → 36.5 h total (+8.5 h) | p.11 |
| Back-prop from T | bond T−40m/1h/1h30; A feed T−1h40/2h30/3h30; side/bead 6/8/10 | p.6 |
| Shifts (optimised premise) | curing 6–15 & 14–23; 胎體/胎面膠 night 20–05 also de-mold/mold-in | p.6 |
| Weekly per press S/M/L | 現況 6/5/3; 只延長班表 6/5.9/4.5; AI 出模時刻 7/6/4.9 | p.6 |
| Lead time | M&L 100 each 13.3 → 9.3 wk; small 2.4 → 2.0 wk | p.6 |
| Detection | 1 min (was 2 h patrol) | p.1 |
| Latency | safety <0.2 s; first alert card ≤2 s (≤100 tokens); full event ≈22 s | p.9 |
| Memory (Spark, 128 GB) | 36–50 GB: model 19, draft 2, KV 4, vLLM/CUDA 5, OS 10, Qdrant 3, app 6 | p.9 |
| Verification | TTFT ≤0.5 s (32k ctx, prefix cache); 3 concurrent ≤4 s each; NVFP4 vs FP8 ≤3 pts; tokens/W; control = FP8 no cache/spec | p.10 |
| Offline tiers | weak net → unaffected; WAN down → phone via LAN; LAN down → touchscreen + light | p.9 |

## Demo scenarios

- `press7_temp_drop` (p.11): 02:14:00 CURE-07 128→122 °C predicted 122±1 → wake; 02:14:01 safety: still ≥120, no stop; H1 steam pressure low; S1 cure 36.5 h, S2 #A-311 due in 3 days, night short 1; S3 #A-311 → CURE-09, take 1 from line 2; 02:16:40 approve; 02:31:00 verify 119.5 °C → H1 rejected, safety core already acted; H2 leak / sensor drift, compare CURE-06/08 (same header H2), SOP 「硫化中斷處置」 → CURE-07 out of service; 02:31:30 supervisor rejects cross-team move, picks night overtime.
- `open_mill_overheat` (p.2): A-BIGOPEN 112 °C rising, water flow low, cause cooling insufficient; ✓ auto notify maintenance (R1); ☐ approve slow raw feed (R3); schedule impact none, 大胎 #07 still 17:00 into press.

## LLM

One commander, OpenAI-compatible: `LLM_MODE=mock|llm`, `BASE_URL`, `MODEL`, `NVIDIA_API_KEY`. Constrained output = `CommanderPlan.model_json_schema()` (vLLM `guided_json` / xgrammar, else `response_format: json_schema`). Mock mode reproduces both scenarios with no model. Spark target: Nemotron 3.5 Lightning 30B-A3B NVFP4 on vLLM.

## Run

```
uv sync                         # Python 3.12 arm64 venv (.python-version pins it)
uv run pytest -q
cd web && npm install && npm run build   # dist/ served by FastAPI
uv run python -m warroom.api    # api builder
```
