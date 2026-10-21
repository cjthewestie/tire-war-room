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
| `warroom/agent/` | agent | `CommanderClient`, `ActionGate`, `AuditLog`, `ApprovalBroker`, `EventStateMachine` | `OpenAICommander`, `MockCommander`, `make_commander`, `RuleGate`, `JsonlAuditLog`, `ApprovalBrokerImpl`, `StateMachine`, `AskAgent` (問戰情室, read-only Q&A on `ASK_TOOLS`) |
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

## Ask the war room (read-only)

`POST /api/ask` → `AskAgent.ask`: PLAN (model returns `AskPlan`, ≤ `ASK_MAX_STEPS` steps; mock = keyword rules) → RUN (code runs only `ASK_TOOLS` via the same `Registry.acall`; anything else → `INTERCEPTED`) → ANSWER (model writes from the step summaries only; failure → template). Audit: `SYSTEM ask` → `LLM_CALL plan` → `TOOL_CALL`/`TOOL_RESULT` (state `ASK`) → `LLM_CALL answer` → `SYSTEM answered`, all with `event_id = ASK-nnnn`. No state machine, no gate: nothing in `ASK_TOOLS` writes, so there is nothing to gate.

## The whole plant (issue #11)

`warroom/sim/world.py` `World` is everything around the 28 machines: orders over time (new / rush orders, quantity and due-date changes; 待排程 → 生產中 → 完成 → 待出貨 → 已出貨), raw materials with reorder points, purchase orders and dock deliveries (sometimes late or short), finished goods and scheduled shipping trucks (10:00 / 16:00), named workers per team and shift with a state and a zone matching the 3D rooms, energy per line (peak flag), inline and final quality inspections (a defect names the machine that made it), and planned maintenance windows plus repair tickets.

- Sources (each item carries `source`): **team dataset** (`data/team_dataset/`, CJ 2026-10-20 「這份資料是隊友做的，當作團隊正式資料集」): the 20 production staff and their roster (shifts, overtime, rest days, S11 sick on the EV-2 night = page 11), mold changes, incident history, daily output history. Ids are mapped in `warroom/data/team_ids.py` (A03→A-BIGOPEN, V07→CURE-07 …; one table to reconcile with the sensor-replay work), and dataset dates are shifted per scenario in `warroom/data/team_dataset.py` (page 11 10/03 ↔ EV-2 10/28, page 2 10/22 ↔ EV-1 10/22). **Generated**: orders arriving over time, materials and purchasing, shipping, energy, inspections, planned maintenance windows, and the maintenance / QC / supervisor staff the dataset does not list.
- `Factory.load_scenario` builds it; `Factory.step` advances it on a fixed 10-s factory-time grid. It only READS the Factory and draws from its own RNG (`Random(seed * 1_000_003 + 11)`), so readings, cure cycles, the schedule and the Factory RNG are unchanged (`tests/test_world.py::test_world_does_not_change_the_plant`, every scenario).
- World-only orders (`#W-…`) do not occupy presses and the seed order book is never edited by the world; planned maintenance does not stop machines. These are views of the plant, not inputs to the scheduler yet.
- API: `GET /api/world` (`WorldSnapshot`); WS `world` messages carry only the sections that changed (`full: true` on the first publish and after a scenario load; nothing extra on connect, clients read `GET /api/world` at start and on reconnect), at most once a wall second (`Runtime._publish_world`).
- Extension point for the agent: `World.add_listener(fn)` receives every `WorldEvent` (`stockout`, `delivery_late`, `delivery_short`, `order_rush`, `defect`, `repair_open` …). Nothing wakes the state machine from it yet; the agent's 9 tools and the Action Gate do not see the world.
- UI: the lower half of the 產線監控 tab (`web/src/components/PlantPanels.tsx`; issue #37 merged the old 工廠全貌 tab in); 3D workers and the dock truck (`web/src/factory3d/People.tsx`). Headless check: `node web/e2e/world.mjs http://127.0.0.1:<port>`. Issue #37 added the Agent page (`web/src/agent/AgentConsolePage.tsx`, the full transcript) and 後台 (`web/src/admin/AdminPage.tsx` + `warroom/api/admin.py`: RAG maintenance, memory notes, status, runs; supervisor PIN and factory network only); check: `node web/e2e/tabs37.mjs http://127.0.0.1:<port>`.

## Plant events, worker tasks and goods status (issue #17)

CJ, 2026-10-20: 「突發狀況(人力調度或機器故障或插單) 早上訂單開工」, 「審核一個plan之後人員要去執行」.

- **Plant inputs**: a scenario step may carry `PlantInput` (`shift_start`, `rush_order`, `absence`) instead of a fault. `Factory.apply_plant_input` adds the rush order to the order book or takes a person off a shift (`shifts()` headcount, the world's roster); it never touches a machine. `Runtime.tick` drains them (`Factory.drain_plant_inputs`) and calls `StateMachine.open_plant_event` → `run_plant`: an Event with `source="plant"`, machine `PLANT`, anomaly kind `shift_start` / `order_rush` / `staffing_short`. CLASSIFY reads the SOP; PLAN reads orders, shifts, gaps, who is on leave, materials and today's mold-ins (`query_orders_shifts`, which now adds `leave`, `materials`, `today` from the world) and writes the card's 已查 lines in code; the commander plans solve → confirm → `notify_teams kind=dispatch` (派工, R2). No new tool: the allow-list is still 9.
- **Solver**: a rush order (priority ≥ 2) that the active plan does not carry at all gets its tyres generated in `_prepare`; its tyres appear in `moves` with `from_press=None`. Plans that carry every rush order solve exactly as before (page 11 unchanged). The card's 交期 row adds per-order completion shifts (`narrate.order_shifts`).
- **Worker tasks** (`warroom/sim/work.py`): after each executed write (approved or gate-allowed R1), `StateMachine._dispatch_tasks` hands the step's validated args, its result and this event's solve moves to `World.tasks.dispatch`; code (`plan_tasks`) builds the tasks — set_cure_time → enter the value on the press panel; confirm → 換模準備 for moved tyres in the next 12 h; 派工 → SOP-02 warm-up in order, 補料, 盤點 (shift start) or 補料 + 換模準備 (rush order); overtime / staff move → the person reports to the team; machine_out → maintenance + QC; info → 宣達. Creation is audited (`AuditKind.TASK`, `generated_by: code`), every status change too (actor `world`). The board assigns a free on-duty person of the right team (deterministic), who walks there (前往中) and does it (執行中) for a fixed time; workers on a task show state 前往中 / 執行中 at the task's place, so the 3D figures walk there. A plant event's VERIFY waits (sim time, ≤ 180 min) until all its tasks are 完成, then closes; machine events report tasks but VERIFY stays reading-based. A revoked staffing step cancels its open tasks.
- **Goods** (`goods_view`): every tyre of the active plan in the next 24 h plus every tyre in a press and the last de-molded ones: 待補料 → 備料中 (A feed .. bond, page 6) → 生產中 (bond .. mold-in) → 硫化中 (cure elapsed / required) → 完成 → 待出貨 → 已出貨, with progress; a tyre whose order has an open 補料 task stays 待補料 (`blocked`). Per order: progress, queued / in press, and how far its next mold-in moved with the last schedule change. World sections `tasks` and `goods`.
- **EV-2 replay fix**: `commander.stable_cure_temp` floors the detector's forecast at the cure band floor (120 °C) when it is a still-falling straight-line extrapolation (team_ev2: 99.8 °C → 131.6 h > 96 h cap → safe stop); page 11's 122 °C is used unchanged.

## Supervisor commands and plan discussion (issue #10)

`POST /api/command {text}` → `StateMachine.open_command` opens an Event with `source="supervisor"`, `command=text` (anomaly kind `supervisor_command`, no alarm, never a machine's open event) and runs CLASSIFY (light) → PLAN → SIMULATE → GATE → [AWAIT_APPROVAL] → EXECUTE → VERIFY (unmeasured) → CLOSED. A command that would move or set a machine (`talk.physical_request`) closes at CLASSIFY with the reason and no tool call.

`POST /api/events/{id}/discuss {text}` → `PlanTalk.discuss`: CLASSIFY (model with a JSON schema; rules for mock mode, model failure, or when the rules read an explicit change the model missed) into chitchat / question / change / unclear / unsupported. A change becomes `PlanConstraint`s validated by code (the press must be named in the text; the window is computed from the words). `StateMachine.discuss_replan` adds them to `Event.constraints`, supersedes the waiting card (`ApprovalStatus.SUPERSEDED`, audited, cannot be decided) and the event goes AWAIT_APPROVAL → REPLAN → SIMULATE → GATE. Code applies the constraints to every `solve_schedule` (`press_blackouts`, `allow_overtime`, `allow_cross_team`), checks the result honours them (`constraint_check`), and the new card carries `version`, `supersedes`, `changes`, `constraints`. Discussion replans do not count toward `max_replans` or failures; `MAX_DISCUSS_REPLANS = 3`. At every gate the agent posts a code-built summary (`plan_summary`).

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

- Team dataset replay (`WARROOM_REPLAY` / `--replay team_ev1|team_ev2|team_both`): `warroom/sim/team_replay.py` plays `data/team_dataset/sensor_readings.csv.gz` (EV-1 A03 → A-BIGOPEN, EV-2 V07 → CURE-07, ids via `warroom/data/team_ids.py`) through `ReplaySource` in the NORMAL scenario, time-shifted to the sim clock. Team SOP chunks are indexed in the KB's `<collection>_team` collection and come back from `sop_search` as `team_hits`; past incidents load into the read-only `warroom.data.team_dataset.IncidentStore` (no agent tool yet).

## LLM

One commander, OpenAI-compatible: `LLM_MODE=mock|llm`, `BASE_URL`, `MODEL`, `NVIDIA_API_KEY`. Constrained output = `CommanderPlan.model_json_schema()` (vLLM `guided_json` / xgrammar, else `response_format: json_schema`). Mock mode reproduces both scenarios with no model. Spark target: Nemotron 3.5 Lightning 30B-A3B NVFP4 on vLLM.

## Run

```
uv sync                         # Python 3.12 venv (.python-version = 3.12, no platform: arm64 on the Mac, linux on the Spark)
uv run pytest -q
cd web && pnpm install && pnpm build   # dist/ served by FastAPI
uv run python -m warroom.api    # api builder
```

## Data path and write access as built (api)

- Page-10 ingest: every tick, `Runtime.tick()` runs the safety core on the readings first (red path, synchronous, before any wait), then the readings take the MQTT hop when `WARROOM_INGEST=mqtt` (`warroom/api/ingest.py`: one message per machine on `warroom/telemetry/<line>/<machine_id>`, Mosquitto in both compose files; broker down or late = direct readings, counted in `/api/health/components` `ingest.mqtt_missed_ticks`), are validated (range/jump) and written to the SQLite time-series table `data/runtime/timeseries.sqlite`, and only then reach detection. The sim stands in for the Modbus/OPC UA gateway; there is no real Modbus/OPC UA client. The INT8 time-series model (page 10) is replaced on this build by the fitted detector in `warroom/detect` (EWMA + curve fit).
- Writes (approvals, schedule rollback, `/api/sim/*`): loopback is the on-site seat; LAN clients need `X-Supervisor-Pin` = `WARROOM_SUPERVISOR_PIN` (the phone page asks for it and keeps it for the tab only; never in a URL). Wrong PINs are throttled per address (5 then a doubling lockout) and globally (20 in 10 min locks remote writes 5 min), each one audited as `intercepted`; `WARROOM_SSL_CERTFILE`/`WARROOM_SSL_KEYFILE` serve TLS; `decided_by`/`via` are set by the server; `via=auto` is server-only. `WARROOM_DEMO_CONTROLS=0` turns `/api/sim/*` off. `WARROOM_LOCAL_HOSTS` adds hosts treated as local; never put a Docker bridge gateway there (everything docker-proxy forwards would count as local). The Spark compose runs the app with `network_mode: host` so the touchscreen really is 127.0.0.1, and requires `WARROOM_SUPERVISOR_PIN` (compose and `install_spark.sh` refuse to run without it); `deploy/spark/smoke_access.sh` checks both seats on the live stack.
- Page-9 tiers: `lan_down` refuses phone/remote approvals (409), lights `header.warning_light` while an approval waits or an event is open (audited on change); `wan_down` changes nothing for the phone (factory LAN).
- Schedule rollback: only a version that was ACTIVE before, never one with new mold-ins on a press that is out of service / stopped / offline (`GET /api/schedule/rollback/preview`), and only with `confirm: true`, which is recorded as an R2/R3 approval through the broker.
- Restart: a graceful stop audits open events as interrupted and does not safe-stop them or roll back the schedule; the next start reopens the same scenario's schedule store. Only an explicit scenario load/reset starts a fresh store.
