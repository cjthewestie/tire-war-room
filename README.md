# 實心胎智慧製造戰情室 · Team Westie

2026 Agentic AI on the Edge Hackathon · 智慧製造組. 企劃書定稿：[`docs/proposal-final.pdf`](docs/proposal-final.pdf)（12 頁，以企劃書為準）。

![dashboard](docs/screenshots/final-dashboard.png)

---

## 中文（團隊用）

**新加入？先看 [`CONTRIBUTING.md`](CONTRIBUTING.md)（下載、執行、開 issue、分支、PR、規範）和 3 分鐘影片 [`docs/onboarding/team-onboarding.mp4`](docs/onboarding/team-onboarding.mp4)。**

### 這是什麼

一個畫面看完整座實心胎工廠：訂單、4 條產線、28 台機台、11 台硫化機，以及每個異常的處置進度。

- **事件偵測**（非 LLM，常駐）看感測器時序，預測會出事才喚醒 AI。目標：異常 1 分鐘內發現（原本人工巡檢 2 小時）。
- **事件狀態機**：分類 → 計畫 → 模擬 → 把關 → 執行 → 驗證 → 結案；預期沒達成就換假設重排，連續失敗就安全停止、保留原排程。
- **事件指揮官** 是唯一的 LLM（Nemotron 3.5 Lightning 30B-A3B NVFP4），只回一份 JSON 步驟計畫。硫化時間由 Arrhenius 公式算、排程由 OR-Tools CP-SAT 解、SOP 從 Qdrant 查 —— 物理和排程數字從不讓 AI 猜。
- **動作把關**（確定性）：白名單、4 級風險 R0–R3、格式檢查、模擬可行才放行。R2/R3 要主管在手機上核准。
- **安全核心**（確定性 PLC 規則）：煉膠 ≥120°C 停機、已加硫 ≥110°C 停機、硫化機 <120°C 扣模。LLM 不能呼叫也不能推翻，不等 AI 直接處置。
- **稽核日誌** 只能追加，有雜湊鏈與簽章，記錄每次狀態轉換、工具呼叫、核准與攔截。
- 全部跑在一台 DGX Spark 上，不連外網。

### 問戰情室（主管提問，只查不改）

手機頁最下方可以直接問：「7 號機現在怎樣？」「今晚夜班缺誰？」「大開煉機過熱怎麼處理？」。走的是同一套 Agent 架構：同一個 Nemotron 先選要查哪些唯讀工具（最多 4 步），程式只執行 `ASK_TOOLS`（讀感測器、異常預測、硫化公式、訂單班表、SOP），再由 Nemotron 只根據查詢結果回答。寫入工具、`solve_schedule`（會存提案排程）和清單外的名稱一律攔下並記 `INTERCEPTED`；每次提問以 `ASK-nnnn` 寫進同一份稽核日誌。LAN 中斷時只有現場觸控螢幕能問。程式：`warroom/agent/ask.py`、`POST /api/ask`，測試 `tests/test_ask.py`。

### 3D 工廠（戰情室負責人視角）

打開 `http://localhost:8000/factory`。你是戰情室負責人，從戰情室桌前出發（面對大螢幕，轉身就是產線）。畫面全部接即時資料（同一條 `/ws`），28 台機台的燈號：綠＝正常、黃＝注意、紅＝異常／停用；靠近機台會浮出簡稱＋溫度。

- 點一下畫面開始（鎖定滑鼠）。`W A S D` 走、`Shift` 跑、滑鼠或 `Q`/`E`（方向鍵）轉向、`Esc` 放開滑鼠。
- 走到機台前按 `F`：開即時讀數（和戰情室的機台抽屜相同）。在戰情室桌前按 `F`：展示控制台，一鍵啟動 p.11「7 號硫化機溫降」、重來、調速度、拔網路測試（和上方展示列同一組 API）。
- `P` 拿起主管手機：手上的就是真的 `/phone` 頁，可直接核准／駁回、打字問戰情室；`P` 或 `Esc` 放下。有新的待核准時手機會震動、響鈴，畫面上方跳通知。
- 低多邊形、全部程式產生，不下載模型。程式：`web/src/factory3d/`；無頭測試：`node web/e2e/factory3d.mjs http://localhost:8000`（截圖在 `web/e2e/screenshots/factory-*.png`）。

### 一行啟動（Mac，Apple Silicon）

需要：[uv](https://docs.astral.sh/uv/)、Node.js 20+ 與 pnpm（`corepack enable pnpm`）。第一次執行會下載 Python 套件、pnpm 套件和 SOP 嵌入模型（數百 MB，之後離線可用）。

```bash
./start.sh            # 模擬模式：不需要任何模型，整個展示用規則指揮官跑完
./start.sh ollama     # Ollama 模式：Nemotron 3.5 Lightning NVFP4（先跑一次 deploy/mac/dev.sh pull，約 23 GB）
```

打開 `http://localhost:8000`（戰情室）、`http://localhost:8000/phone`（主管手機）和 `http://localhost:8000/factory`（3D 工廠）。用真手機從廠內 Wi-Fi 核准時，先設 `WARROOM_SUPERVISOR_PIN=<8 碼以上>` 再啟動，手機頁會要求輸入。

不開瀏覽器、只看時間軸：

```bash
uv run python -m warroom.api demo --scenario press7_temp_drop --seconds 1200 --auto-approve
```

### 在 DGX Spark 上跑

```bash
sudo WARROOM_SUPERVISOR_PIN=<pin> deploy/bin/install_spark.sh   # 趁還有網路：拉映像、下載權重、裝 systemd、開機防火牆
sudo systemctl start warroom-stack                              # vLLM（NVFP4 + DSpark 推測解碼 + prefix cache）+ Qdrant + MQTT + app
curl -s 127.0.0.1:8000/api/health
```

- 堆疊：`deploy/spark/docker-compose.yml`、vLLM 參數 `deploy/spark/vllm-serve.sh`、環境 `deploy/spark/.env.example`。
- 開機自啟 + 模型預載 + watchdog：`deploy/systemd/`、`deploy/bin/watchdog.sh`、`deploy/bin/preload.sh`。
- 離線更新 / 一鍵回復：`deploy/update/offline_update.sh`、`deploy/update/rollback.sh`。
- 沙箱（NemoClaw/OpenShell）：`deploy/openshell/run_sandboxed.sh`；對外網路白名單：`deploy/network/`。
- 安全核心獨立程序（PLC 替身）：`uv run python -m warroom.safety.plc --demo`。
- 第 10 頁量測：`bench/spark_ablation.sh`（FP8 無快取無推測解碼對照組 + 各項最佳化貢獻），`bench/py.sh -m bench.report` 產生 `bench/results/SUMMARY.md`。

### 決賽展示流程（對應企劃書第 11 頁）

完整台詞與點擊順序：[`docs/DEMO-SCRIPT.md`](docs/DEMO-SCRIPT.md)。精簡版：

1. 戰情室在展示列點 **7 號硫化機溫降（p.11）**，速度 **10×**。用真模型時先暖機（第一次載入約 82 秒），見 DEMO-SCRIPT。
2. [02:14:00] 事件偵測：7 號 128→122°C，預測穩定 122±1 → 喚醒指揮官。[02:14:01] 安全核心：仍在 120–130，不停機。
3. 指揮官假設 H1 蒸汽壓不足；拆 S1 硫化公式、S2 訂單班表 → S3 排程 → S4 確認（需核准）→ S5 通知；預期 30 分內 ≥121°C。
4. S1：122°C 總需 **36.5 h（+8.5 h）**；S2：#A-311 交期 3 天後、10/03 夜班缺 1 人；S3：**#A-311 移到 9 號機**、夜班從 2 線調 1 人，交期全部滿足。
5. 手機跳出第一輪核准（R3）→ 選 **B 從 2 線調 1 人** → 核准，寫入稽核日誌。
6. 切 **60×**。[02:28:50] 掉破 120°C，安全核心直接扣模（不等 AI）。[02:31:00] 程式驗證 119.5°C → **H1 不成立**。
7. 指揮官換 **H2 持續洩壓或感測器漂移**，對照同管線 6、8 號機，查 SOP〈硫化中斷處置〉→ 7 號機停用、重排。
8. 手機第二輪「CURE-07溫度下降，需重排」（將做：7 號機停用）→ 主管改選 **A 夜班加班** → 核准；決定與理由寫入紀錄，排程 v3 生效，舊版可一鍵回復。
9. **拔網路線**：在 Spark 上拔 WAN 線，戰情室照常、手機走廠內 LAN；Mac 上點 **WAN 斷 / LAN 斷** 示範第 9 頁三層離線（LAN 斷 → 現場觸控螢幕 + 警示燈）。正式判定：`sudo -E bench/py.sh -m bench.unplug_test --unplug <wan介面> --app-url http://127.0.0.1:8000`。
10. 切到 **排程與人力** 分頁看重排後的甘特圖。

備用展示：**大開煉機過熱 (p.2)** —— A 線大開煉機 112°C 上升，✓ 已自動通知維修冷卻水路、☐ 待核准放慢生料投入、排程影響「無，大胎 #07 仍 17:00 入硫化機」。

---

## English

### What it is

A single-screen war room for a solid-tire plant (4 lines, 26 processes, 28 machines, 11 curing presses — the bottleneck). Always-on event detection wakes a deterministic event state machine; one LLM (the Event Commander) returns a JSON step plan; read/compute tools run automatically; write tools pass a deterministic Action Gate and, at R2/R3, a supervisor's phone approval; a deterministic Safety Core acts on its own and cannot be called or overridden by the LLM. Everything runs on one DGX Spark with no internet.

### Quick start (Mac, Apple Silicon)

Needs [uv](https://docs.astral.sh/uv/), Node.js 20+ and pnpm (`corepack enable pnpm`). The first run downloads Python and pnpm packages and the SOP embedding model (a few hundred MB); after that it runs offline.

```bash
./start.sh            # mock mode: no model, the whole demo runs on the rule-based commander
./start.sh ollama     # Ollama mode: Nemotron 3.5 Lightning NVFP4 (run deploy/mac/dev.sh pull once, ~23 GB)
```

Open `http://localhost:8000` (dashboard), `http://localhost:8000/phone` (supervisor phone) and `http://localhost:8000/factory` (3D factory). For a real phone on the LAN, start with `WARROOM_SUPERVISOR_PIN=<8+ chars>`. Other knobs: `PORT` (mock mode only: in `./start.sh ollama` mode `deploy/mac/dev.sh` reuses `PORT` as the Ollama port, so the app ends up on :11435; see the demo script for the manual command), `SCENARIO` (`normal`, `press7_temp_drop`, `open_mill_overheat`, `bad_data`, `concurrent_3`), `SIM_SPEED`, and for any OpenAI-compatible server `LLM_MODE=llm BASE_URL=... MODEL=... [NVIDIA_API_KEY=...]`.

Manual steps, if you prefer: `uv sync` · `cd web && pnpm install --frozen-lockfile && pnpm build` · `uv run python -m warroom.api serve`. Tests: `uv run pytest -q`. Headless timeline: `uv run python -m warroom.api demo --scenario press7_temp_drop --seconds 1200 --auto-approve`.

### Running on the DGX Spark

See the Chinese section above for commands. `deploy/bin/install_spark.sh` pulls images and weights while online, writes `/opt/warroom/releases/<ver>`, installs the systemd units (stack, preload, watchdog, firewall) and refuses to finish without a supervisor PIN. After that the box boots straight into the war room with the model preloaded and all non-LAN egress dropped.

### Finals demo

Step by step, with what to click and what to say: [`docs/DEMO-SCRIPT.md`](docs/DEMO-SCRIPT.md). It follows page 11 minute by minute and ends with the page-9 unplug.

### The four judging thresholds → code

| Threshold (p.7) | Where it lives |
|---|---|
| 1. Goal & decomposition by the commander (S1 cure ∥ S2 orders → S3 solve depends on both → S4 approve → S5 notify) | `warroom/agent/commander.py` (OpenAI-compatible client, `CommanderPlan` JSON schema, xgrammar/`json_schema` constrained output, reasoning off), `warroom/agent/state_machine.py` (`resolve_refs` for `$S1.total_cure_h`, dependency-ordered SIMULATE), `CommanderPlan` / `PlanStep` in `warroom/contracts.py` |
| 2. Tools opened by state | `TOOLS_BY_STATE`, `READ_COMPUTE_TOOLS`, `WRITE_TOOLS` in `warroom/contracts.py`; enforced in `StateMachine._call` (`warroom/agent/state_machine.py`, off-state calls logged `INTERCEPTED`); tools in `warroom/tools/` (`cure.py` Arrhenius, `impl.py`, `registry.py`), CP-SAT in `warroom/schedule/solver.py`, Qdrant SOP KB in `warroom/kb/` |
| 3. State & dynamic adjustment (miss → swap hypothesis & replan; retry once → SOP path; solver timeout → backup; 2 failures → keep schedule) | `ALLOWED_TRANSITIONS`, `MAX_TOOL_RETRIES=1`, `MAX_CONSECUTIVE_FAILURES=2` in `warroom/contracts.py`; code-only VERIFY and REPLAN in `warroom/agent/state_machine.py`; `FallbackScheduler` (CP-SAT → heuristic, `fell_back=True`) in `warroom/schedule/solver.py`; SAFE_STOP keeps the active schedule |
| 4. Permissions & risk (approval for feed pause / staff moves, 4 levels, no actuator permission, intercepts logged, feasible-only, one-click rollback) | `RuleGate` in `warroom/agent/gate.py` (R0–R3, allow-list, format, simulation-feasible); `ApprovalBrokerImpl` in `warroom/agent/approvals.py`; append-only hash-chained `JsonlAuditLog` in `warroom/agent/audit.py`; versioned `FileScheduleStore.rollback` in `warroom/schedule/store.py`; Safety Core in `warroom/safety/core.py` (and `plc.py` as a separate process; a test forbids it importing agent/tools/api/LLM clients); no actuator/PLC write tool exists anywhere |

### Ask the war room (read-only supervisor Q&A)

The phone page has a 問戰情室 box. It is not a second agent: the same Nemotron picks up to 4 read steps (JSON, schema-constrained), code runs only `ASK_TOOLS` (read sensors, predict, cure formula, orders/shifts, SOP) through the same registry, and the model then answers from those results only (numbers copied, never estimated). Write tools, `solve_schedule` (it stores a PROPOSED version) and names off the allow-list are refused and logged `INTERCEPTED`; every question, call and answer goes into the same hash-chained audit log under an `ASK-nnnn` id. Same session rule as approvals (PIN on the LAN, on-site only when the LAN is down). Code also checks the answer: a number that is not in the tool results replaces the model's answer with the results, and a change request (停掉, 重排, 調人...) never gets a model-written answer. The check covers numbers only, not wording (a real-model run once called press 7 「正常運作」). Mock mode uses keyword rules and a template answer; an invalid plan or a model failure falls back to them. Real Nemotron on the Mac (Ollama, 2026-10-18): 5 questions, 0 fallbacks, 0.5–3.2 s each once loaded. Code: `warroom/agent/ask.py`, `POST /api/ask`; tests: `tests/test_ask.py`.

### 3D factory (the war-room lead's view)

Open `http://localhost:8000/factory`. You are the war-room lead, starting at the war-room desk (facing the big screen; turn around for the lines). Everything is live data from the same `/ws`: each of the 28 machines has a status light (green normal, amber warning, red alarm / out of service) and shows its short name and temperature when you walk near.

- Click to start (pointer lock). `W A S D` walk, `Shift` run, mouse or `Q`/`E` (arrow keys) to turn, `Esc` releases the mouse.
- `F` at a machine opens its live readings (the dashboard's machine drawer). `F` at the war-room desk opens the demo controls: start the p.11 press-7 temperature drop, reset, speed, network tiers (the same API as the dashboard's demo strip).
- `P` raises the supervisor phone: it is the real `/phone` page, so you can approve / reject and type into 問戰情室; `P` or `Esc` lowers it. A new pending approval buzzes the phone, rings and shows a toast.
- Low-poly and fully procedural, no model downloads. Code: `web/src/factory3d/`; headless check: `node web/e2e/factory3d.mjs http://localhost:8000` (screenshots in `web/e2e/screenshots/factory-*.png`).

### Measured numbers vs targets

Only numbers actually measured are listed. Mac rows are an Apple M4 Max 64 GB running Ollama 0.33 (MLX) with `nemotron-3.5-lightning:30b-a3b-nvfp4` — a stand-in for the Spark, not the Spark. Raw JSON is in `bench/results/`, the generated table in `bench/results/SUMMARY.md`.

| Metric | Target | Measured | Where | Verdict |
|---|---|---|---|---|
| Test suite | green | 582 passed, 3 skipped (13 min) | Mac | pass |
| Page-11 event end to end, real model | closes, numbers match p.11 | closed; 36.5 h (+8.5 h), #A-311 → 9, night short 1, verify 119.5 °C → H2 | Mac, Ollama | pass |
| Page-6 weekly output per press S/M/L (DES check of the proposal table) | 6/5/3 · 6/5.9/4.5 · 7/6/4.9 | 6/5/3 · 6/6/4.82 · 7/6/4.89 | `warroom/schedule/capacity.py` | within ±0.1 except M "extend shifts" (6 vs 5.9) |
| Page-6 lead time M&L / small | 13.3 → 9.3 wk / 2.4 → 2.0 wk | 13.2 → 9.4 wk / 2.5 → 2.1 wk (DES) | same | close; table shows the proposal values |
| Safety core decision time (28 machines per tick) | < 0.2 s | p99 0.058 ms, max 0.104 ms (n=1500) | Mac, compute only, not sensor→PLC | pass |
| First alert card, cold, p90 | ≤ 2 s | 1.85 s (n=5, ~5.6k-token prompt) | Mac, idle host | pass |
| Full event, real state machine, p50 | ≈ 22 s | 16.2 s (3/3 closed, plans 100 % valid) | Mac | pass |
| TTFT warm 32k, p90 | ≤ 0.5 s | 4.51 s (p50 0.118 s) | Mac | fail on Mac; Spark pending |
| 3 concurrent anomalies, worst alert | each ≤ 4 s | 22.7 s; Ollama/MLX queued instead of batching (sequential each ≈ 2.1 s) | Mac | fail on Mac; needs vLLM batching on the Spark |
| Decode throughput | publish | 99.8 tok/s single stream | Mac | measured |
| Unplug: alert offline / outbound connections | ≤ 2 s / 0 | 0 outbound connections in 120 s + one event; first commander card 6.83 s (commander TTFT 5.9 s on a 5.2k-token prompt) | Mac, process-level `sandbox-exec` block, no cable pulled | outbound pass; alert ≤ 2 s fail on Mac |

**Pending on the DGX Spark** (not measured, no Spark here): memory band 36–50 GB with the full stack at 32k context (the Mac run was UNJUDGED: app not running, model at 16k); TTFT ≤ 0.5 s at 32k with vLLM prefix cache; 3 concurrent ≤ 4 s each with vLLM batching; NVFP4 vs FP8 ≤ 3 points on vLLM (Mac Ollama NVFP4 vs MXFP8 runs exist but are marked stale after bench code changed); tokens/W (needs a wall-plug meter); the FP8 / no-cache / no-spec-decode control group and per-optimisation contributions (`bench/spark_ablation.sh`); the physical cable-pull unplug with the nftables block.

### Known gaps

- No DGX Spark was available: every model number above is from a Mac/Ollama stand-in. The vLLM, NVFP4 kernel, DSpark speculative decoding and nftables paths are written and unit-tested but not run on GB10 hardware.
- The sensor side is simulated (data replay + fault injection). There is no real Modbus/OPC UA client; the sim stands in for the gateway and readings take an MQTT hop only with `WARROOM_INGEST=mqtt`.
- The page-10 INT8 time-series model is replaced by a fitted detector (EWMA + curve fit) in `warroom/detect/`.
- Orders and shifts come from seed data or an Excel import (`data/orders_seed.xlsx`); there is no MES link, as the proposal states.
- NeMo Agent Toolkit is an optional plugin (`warroom/natkit/`, `deploy/nat/run.sh`); it is not a base dependency because its profiler pins older pandas/rich.
- OpenShell/NemoClaw sandboxing (`deploy/openshell/`) needs the OpenShell CLI and gateway on the Spark; on the Mac the offline proof is a process-level `sandbox-exec` block.
- On the Mac the 3-concurrent and 32k-TTFT targets fail: Ollama on MLX does not batch concurrent requests and its prefix cache is less consistent than vLLM's. These are judged again on the Spark.
- The first run needs internet once to download the SOP embedding model (fastembed); the Spark installer pre-fetches it.
- Page 11 round 1: the solver moves #A-311 to press 9 and, as a knock-on, one later #M-201 tire from press 9 to press 8, so the timeline and the round-2 已做 row also name 「#M-201 改 8 號」. #A-311 is a large (L) tire in the seed and the Excel workbook; its 34 h cure pushes press 9's own queue. Seeding it as a medium tire removes the knock-on but breaks the page-2 100/100/100 totals and makes round 2 move #A-311 again, so the seed is left as is.
- Activation energy 85 kJ/mol is the proposal's assumed value, not a measured compound property.

### Layout

`warroom/` Python (contracts, factory model, sim, detect, safety, tools, schedule, kb, agent, api) · `web/` Vite + React dashboard and phone page · `deploy/` Mac and Spark stacks, systemd, firewall, sandbox, offline update · `bench/` page-9/10 benchmarks · `tests/` pytest · `docs/` proposal, architecture, demo script, screenshots · `legacy/` the superseded first prototype.
