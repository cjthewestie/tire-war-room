# 決賽 3 分鐘展示腳本 — 實心胎智慧製造戰情室

Follows proposal page 11 (7 號硫化機溫降) and page 9 (unplug). Times on the left are wall-clock seconds into the talk; times in brackets are factory (sim) time shown in the header.

## Before you go on stage (5 min)

1. Start the stack.
   - Mac, no model: `./start.sh`
   - Mac, real model: `./start.sh ollama` (Nemotron 3.5 Lightning NVFP4 on a project Ollama, :11435). Do not set `PORT` with this mode: `deploy/mac/dev.sh` reuses `PORT` for the Ollama port, so `PORT=8772 ./start.sh ollama` puts the app on :11435 instead of :8772. For another app port, run `deploy/mac/dev.sh ollama`, then `LLM_MODE=llm BASE_URL=http://127.0.0.1:11435/v1 MODEL=nemotron-3.5-lightning:30b-a3b-nvfp4 PORT=8772 uv run python -m warroom.api serve`.
   - Real model: warm it up before you go on. The first call after the project Ollama starts loads 22 GB and took 82 s on the Mac (rehearsal 2026-10-04); after that each plan takes about 10 s. Either run the 7 號硫化機溫降 scenario once end to end, or `curl -s 127.0.0.1:11435/api/generate -d '{"model":"nemotron-3.5-lightning:30b-a3b-nvfp4","prompt":"hi","stream":false,"options":{"num_predict":5}}'`. The model then stays loaded for 24 h.
   - DGX Spark: it boots by itself (`warroom-stack.service`); check `curl -s 127.0.0.1:8000/api/health`.
2. Laptop/touchscreen: open `http://localhost:8000` full screen (1440 wide or larger). Header must show the `即時・規則模式指揮官` chip (mock) or `即時・LLM 指揮官` (model).
3. Supervisor phone on the factory Wi-Fi: open `http://<host LAN IP>:8000/phone`, enter the supervisor PIN (`WARROOM_SUPERVISOR_PIN`), tap 開啟核准提醒. The on-box screen at 127.0.0.1 needs no PIN.
4. In the 展示 strip, click 正常運轉, then 1×. The network buttons sit on ● 在線.
5. If the unplug segment is live on the Spark: know which cable is the uplink (WAN) and which is the factory LAN switch. Run `sudo -E bench/py.sh -m bench.unplug_test --unplug <wan-iface> --app-url http://127.0.0.1:8000 --watch 120` from a second terminal only if you want the counted verdict on screen.

## The walkthrough

| Wall time | Click / do | Say |
|---|---|---|
| 0:00 | Dashboard on 產線監控, normal running. Point at the four panels. | 「這是一個畫面看完整座工廠：訂單、四條產線、28 台機台、11 台硫化機。以前靠每 2 小時人工巡檢，現在 1 分鐘內偵測。」 |
| 0:15 | Point at ① 訂單進度 (小 38/100 · 中 12/100 · 大 8/100) and ④ 機台狀態. | 「硫化機是瓶頸，中大胎只有 4 台能做。我們的排程從硫化機出模時刻往回推，所以每個異常都要先問：會不會讓大胎遲交？」 |
| 0:25 | In the 展示 strip click **7 號硫化機溫降（p.11）**, then **10×**. | 「現在凌晨兩點，只有夜班。7 號機正在硫化中胎，已經 10 小時。」 |
| 0:35 | [02:14:00] Header count chip turns red (正常 27 台・異常 1 台); press 7 tile red; orange ⚡ 首張告警卡：7 號機異常 line appears (about 12 s after the click). | 「事件偵測（不是 LLM，常駐的時序模型）看到 128 掉到 122°C，預測穩定在 122±1，才喚醒指揮官。安全核心檢查：還在 120–130，不停機。」 |
| 0:50 | In ③ 目前事件, click the collapsed **事件時間軸（4）** line to open it. Point at H1 and the 拆步驟 row (S1..S5). With the model warm they are there about 25 s after the click. | 「唯一的 LLM 是事件指揮官。它只回一份 JSON 步驟計畫：假設 H1 蒸汽壓不足會穩定；S1 硫化公式、S2 訂單班表，S3 排程等 S1、S2 都完成。」 |
| 1:05 | Point at 36.5 h (+8.5 h) and the line 排程影響：待核准 v2：#M-201 改 8 號、#A-311 改 9 號. | 「36.5 小時是 Arrhenius 公式算的，換模是 OR-Tools 求解器排的 —— 物理和排程數字從不讓 AI 猜。下一模 #A-311 移到 9 號機，夜班缺 1 人，交期全部滿足。」 |
| 1:20 | [02:14:26] Phone buzzes (the card is usually up by 0:55; it waits). Show the phone card (R3, 第一輪核准). Pick **B 從 2 線調 1 人**, tap **核准 B**. | 「寫入動作要過動作把關：白名單、四級風險、格式、模擬可行。這是 R3，必須主管核准。主管在手機上按一下，決定回到狀態機，寫進稽核日誌。」 |
| 1:35 | Click **60×**. | 「狀態機說好 30 分鐘內要回到 121°C 以上，驗證是程式做的，不是 AI。」 |
| 1:50 | [02:28:50] Safety core line: 119.9°C < 120°C → 扣模 (about 8 s after 60×). | 「掉破 120 的那一刻，安全核心直接處置。它是確定性規則，LLM 不能呼叫、不能推翻，也不等 LLM。」 |
| 2:00 | [02:31:00] Timeline: 驗證 119.5°C → H1 不成立 → H2. | 「預期沒達成 → H1 被推翻。指揮官換假設 H2：持續洩壓或感測器漂移，對照同一條蒸汽管的 6、8 號機，查 SOP〈硫化中斷處置〉→ 7 號機停用、重排。」 |
| 2:15 | [02:31:22] Phone card 第二輪核准「CURE-07溫度下降，需重排」, 將做：7 號機停用 (it arrives at about 1:55; it waits). Pick **A 夜班加班**, tap **核准 A** (the reason 否決「從 2 線調 1 人」，改選「夜班加班」 is written for you; tap 修改 instead to type your own). | 「這次主管不同意跨組調人，選夜班加班。決定和理由都進稽核日誌，系統照主管的決定執行，排程舊版保留，可一鍵回復。」 |
| 2:30 | Pull the WAN cable (Spark) — or click **WAN 斷**, then **LAN 斷** on the Mac. | 「拔掉對外網路：模型、知識庫、求解器全在這台 DGX Spark 上，戰情室照常運作，手機走廠內 LAN。連 LAN 都斷了，現場觸控螢幕加警示燈接手。」 |
| 2:45 | Click the 人力排程 tab: Gantt v3 生效, press 7 停用・扣模待 QC, #A-311 on 9, 回復上一版 button top right. | 「這就是重排後的排程。偵測 2 小時變 1 分鐘；中大胎交期 13.3 週降到 9.3 週。全部在一台 240 W 的邊緣主機上。」 |
| 3:00 | Click 產線監控 to end on the dashboard. Click ● 在線 off stage before the next run. | 「謝謝。」 |

## If something goes wrong

- No approval card after 30 s at 10x: click 7 號硫化機溫降 again. Loading a scenario starts it fresh with a new schedule store.
- Real model slow or down: with `LLM_FALLBACK_TO_MOCK` on (the default) the commander falls back to the rule-based plan and the audit log records it. The demo still runs end to end; say so honestly. For a guaranteed run, start in mock mode (`./start.sh`). Each model call waits up to `LLM_TIMEOUT_S` (default 60 s). A commander plan may use up to `COMMANDER_MAX_TOKENS` (default 4096; the live p.11 plan is ~1.3k); a plan cut at that cap is logged with cause `truncated` (strict reason 「模型輸出超過長度上限」). With `--strict` there is no rule fallback: a model timeout or failure ends the event in 安全中止 with the reason 「模型逾時，未產生計劃（strict 模式不使用規則備援）」, and `/api/health` shows `strict: true`.
- Real model, different order: in 1 of 3 rehearsal runs (2026-10-04) the model set its H1 expectation to ≥123°C instead of ≥121°C. The reading of 122°C misses it at once, so H1 fails at about 02:19, the second phone card comes before the 02:28:50 扣模, and S3 moved #A-311 to 8 號 instead of 9. Everything still runs; tell it in the order the screen shows. For the exact page-11 order every time, use mock mode.
- Times above are from rehearsals against the real model on the Mac at 10× then 60×. Factory time keeps running while you talk, so at 1:20 the header shows about 02:18 and at 2:15 about 02:51.
- Phone says PIN wrong: use the on-box screen (127.0.0.1) to approve; it needs no PIN.
- Page-2 backup demo: 展示 → 大開煉機過熱 (p.2). A-BIGOPEN 112°C rising, ✓ 已自動 通知維修冷卻水路, ☐ 待核准 放慢生料投入, 排程影響 「無，大胎 #07 仍 17:00 入硫化機」.
