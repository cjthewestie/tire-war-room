# Tire War Room

**An offline AI agent for a factory floor, built so the AI can never touch a machine on its own.**

Tire War Room watches every machine in a solid-tire plant, notices problems before an operator would, plans a response with an LLM, and asks a supervisor to approve it from their phone. It runs on one edge box with no internet. A fixed set of rules decides what the AI may do, and the safety rules sit outside the AI's reach.

![Tire War Room dashboard](docs/screenshots/final-dashboard.png)

[繁體中文說明 → README.zh-TW.md](README.zh-TW.md)

Built by Team Westie for the 2026 Agentic AI on the Edge Hackathon (smart manufacturing track).

---

## Why this exists

Most agent frameworks help an LLM call tools. In a factory, that's the dangerous part: a wrong tool call can ruin a batch, hurt someone, or stop a line. This project explores the opposite question: **how little can you let the LLM do while still getting value from it?**

The answers it uses:

- **The LLM only writes plans.** It returns a JSON step plan, and nothing else. Code runs the steps.
- **Physics and scheduling are never guessed.** Cure time comes from the Arrhenius formula, schedules from the OR-Tools CP-SAT solver, procedures from a Qdrant SOP search.
- **Every write goes through a gate.** An allow-list, four risk levels (R0–R3), format checks, and a simulation that must say "feasible". R2 and R3 need a human to approve on their phone.
- **Safety rules run without the AI.** A rule-based safety core (a PLC stand-in) stops or holds machines on its own. The LLM can't call it or override it, and a test fails if the safety code ever imports the agent.
- **Everything is logged.** An append-only audit log with a hash chain records every state change, tool call, approval and blocked request.
- **No machine-moving tool exists.** There is no actuator or PLC write tool anywhere in the code.

## What it does

- **Detects** trouble from sensor time series (no LLM, always on) and wakes the agent only when an anomaly is predicted.
- **Runs each incident through a state machine:** classify → plan → simulate → gate → execute → verify → close. If the result misses the target, it swaps its hypothesis and replans. After two failures in a row it stops safely and keeps the old schedule.
- **Talks to the supervisor:** approve or reject plans, discuss a plan in plain language ("don't move people between teams", "pick option A"), give new orders, or ask read-only questions ("how is press 7 doing?").
- **Dispatches people, not machines:** approved steps become worker tasks you can watch in a live 3D factory.

## Quick start (5 minutes, no GPU, no model)

Needs Python 3.12 with [uv](https://docs.astral.sh/uv/), and Node.js 20+ with pnpm (`corepack enable pnpm`). Tested on macOS (Apple Silicon).

```bash
git clone https://github.com/cjthewestie/tire-war-room.git
cd tire-war-room
./start.sh            # mock mode: a rule-based commander stands in for the LLM
```

Then open:

| Page | URL |
|---|---|
| War-room dashboard | http://localhost:8000 |
| Supervisor phone | http://localhost:8000/phone |
| 3D factory (WASD to walk) | http://localhost:8000/factory |

To see a full incident, choose **7 號硫化機溫降** (press 7 temperature drop) on the demo strip and approve the card on the phone page. The first run downloads Python and JS packages and a small embedding model (a few hundred MB). After that it runs offline.

Without a browser:

```bash
uv run python -m warroom.api demo --scenario press7_temp_drop --seconds 1200 --auto-approve
```

### With a real model

```bash
deploy/mac/dev.sh pull   # once: Nemotron 3.5 Lightning NVFP4 via Ollama, ~23 GB
./start.sh ollama
```

Any OpenAI-compatible server works: `LLM_MODE=llm BASE_URL=... MODEL=... ./start.sh`.

### On an NVIDIA DGX Spark

```bash
sudo WARROOM_SUPERVISOR_PIN=<pin> deploy/bin/install_spark.sh   # while online: images, weights, systemd, firewall
sudo systemctl start warroom-stack                              # vLLM + Qdrant + MQTT + app
curl -s 127.0.0.1:8000/api/health
```

After install, the box boots straight into the war room with the model preloaded and all non-LAN traffic blocked. Details: [README.zh-TW.md](README.zh-TW.md) ("在 DGX Spark 上跑").

## How it works

```
sensors ─▶ detector (no LLM) ─▶ state machine ─▶ Event Commander (LLM: JSON plan only)
                                     │
                                     ├─▶ read / compute tools (cure formula, CP-SAT, SOP search)
                                     ├─▶ Action Gate (allow-list, R0–R3, simulate) ─▶ supervisor phone (R2/R3)
                                     └─▶ hash-chained audit log
safety core (rules, separate process) ─▶ stops / holds machines directly, never called by the LLM
```

| Idea | Code |
|---|---|
| Plan schema and contracts | `warroom/contracts.py` |
| Event Commander (LLM client, constrained JSON) | `warroom/agent/commander.py` |
| Incident state machine, verify and replan | `warroom/agent/state_machine.py` |
| Tools opened per state | `TOOLS_BY_STATE` in `warroom/contracts.py`, tools in `warroom/tools/` |
| Action Gate (R0–R3) | `warroom/agent/gate.py` |
| Approvals | `warroom/agent/approvals.py` |
| Audit log | `warroom/agent/audit.py` |
| Safety core | `warroom/safety/core.py`, `warroom/safety/plc.py` |
| Scheduler (CP-SAT with fallback) | `warroom/schedule/solver.py` |
| SOP knowledge base | `warroom/kb/` |
| Read-only Q&A | `warroom/agent/ask.py` |

A longer walk-through is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Measured numbers

Only measured numbers are listed. All of them come from a **Mac (Apple M4 Max, 64 GB, Ollama, Nemotron 3.5 Lightning NVFP4), not a DGX Spark.** Raw results are in `bench/results/`.

| Metric | Target | Measured on Mac |
|---|---|---|
| Safety core decision, 28 machines | < 0.2 s | p99 0.058 ms |
| First alert card, cold, p90 | ≤ 2 s | 1.85 s |
| Full incident, p50 | ≈ 22 s | 16.2 s (3/3 closed, all plans valid) |
| Outbound connections while offline | 0 | 0 in 120 s |
| 3 anomalies at once, worst alert | ≤ 4 s each | 22.7 s: **fails** (Ollama queues instead of batching; vLLM on Spark not yet measured) |

The full table, with pending Spark measurements, is in [README.zh-TW.md](README.zh-TW.md#measured-numbers-vs-targets).

## Status and limits

This is a hackathon project, not production software.

- **The sensors are simulated** (data replay plus fault injection). There is no Modbus or OPC UA client yet.
- **Spark numbers are pending.** The vLLM, NVFP4 and firewall paths are written and unit-tested but not yet measured on GB10 hardware.
- **The plant model is specific to one solid-tire factory** (4 lines, 28 machines, 11 curing presses). Adapting it to another plant currently means editing `warroom/factory.py` and parts of the state machine.
- **The data is synthetic.** `data/team_dataset/` was made by the team for this project.

The full list is in [README.zh-TW.md](README.zh-TW.md#known-gaps).

## Project layout

```
warroom/   Python: contracts, factory model, sim, detect, safety, tools, schedule, kb, agent, api
web/       Vite + React dashboard, phone page and 3D factory
deploy/    Mac and DGX Spark stacks, systemd, firewall, sandbox, offline update
bench/     benchmarks and results
tests/     pytest
docs/      proposal, architecture, demo script, screenshots
```

## Tests

```bash
uv run pytest -q
cd web && pnpm build && pnpm lint
```

## Contributing

Issues and pull requests are welcome. The team workflow (issues, branches, PR checks) is in [`CONTRIBUTING.md`](CONTRIBUTING.md), currently in Traditional Chinese. Two rules matter most:

1. No tool or code path may move a machine. All writes go through the Action Gate.
2. `warroom/safety/` must never import the agent, tools, API or an LLM client.

## License

(unknown — needs CJ and the team before the repo goes public)

## Credits

Team Westie (git authors): CJ, Wayne IMC, LUFI319, Shen-EN-DE, Joy_Shen. Real names and roles: (unknown — needs CJ). Built with NVIDIA Nemotron, vLLM, OR-Tools, Qdrant, FastAPI, React and three.js.
