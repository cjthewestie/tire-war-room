# 決賽 3 分鐘展示腳本 — 實心胎智慧製造戰情室

Follows proposal page 11 (7 號硫化機溫降) and page 9 (unplug). Times on the left are wall-clock seconds into the talk; times in brackets are factory (sim) time shown in the header.

## Before you go on stage (5 min)

1. Start the stack.
   - Mac, no model: `./start.sh`
   - Mac, real model: `./start.sh ollama` (Nemotron 3.5 Lightning NVFP4 on a project Ollama, :11435)
   - DGX Spark: it boots by itself (`warroom-stack.service`); check `curl -s 127.0.0.1:8000/api/health`.
2. Laptop/touchscreen: open `http://localhost:8000` full screen (1440 wide or larger). Header must show the `即時・規則模式指揮官` chip (mock) or `即時・LLM 指揮官` (model).
3. Supervisor phone on the factory Wi-Fi: open `http://<host LAN IP>:8000/phone`, enter the supervisor PIN (`WARROOM_SUPERVISOR_PIN`), tap 開啟核准提醒. The on-box screen at 127.0.0.1 needs no PIN.
4. Click 展示 → 正常運轉, speed 1x. Network chip on 在線.
5. If the unplug segment is live on the Spark: know which cable is the uplink (WAN) and which is the factory LAN switch. Run `sudo -E bench/py.sh -m bench.unplug_test --unplug <wan-iface> --app-url http://127.0.0.1:8000 --watch 120` from a second terminal only if you want the counted verdict on screen.

## The walkthrough

| Wall time | Click / do | Say |
|---|---|---|
| 0:00 | Dashboard on 產線監控, normal running. Point at the four panels. | 「這是一個畫面看完整座工廠：訂單、四條產線、28 台機台、11 台硫化機。以前靠每 2 小時人工巡檢，現在 1 分鐘內偵測。」 |
| 0:15 | Point at ① 訂單進度 (小 38/100 · 中 12/100 · 大 8/100) and ④ 機台狀態. | 「硫化機是瓶頸，中大胎只有 4 台能做。我們的排程從硫化機出模時刻往回推，所以每個異常都要先問：會不會讓大胎遲交？」 |
| 0:25 | Click 展示 → **7 號硫化機溫降 (p.11)**, then **10x**. | 「現在凌晨兩點，只有夜班。7 號機正在硫化中胎，已經 10 小時。」 |
| 0:35 | [02:14:00] Header turns 異常 1 台; press 7 tile red; orange 首張告警卡 line appears. | 「事件偵測（不是 LLM，常駐的時序模型）看到 128 掉到 122°C，預測穩定在 122±1，才喚醒指揮官。安全核心檢查：還在 120–130，不停機。」 |
| 0:50 | Open ③ 中央說明面板 → 事件時間軸. Point at H1 and S1..S5. | 「唯一的 LLM 是事件指揮官。它只回一份 JSON 步驟計畫：假設 H1 蒸汽壓不足會穩定；S1 硫化公式、S2 訂單班表，S3 排程等 S1、S2 都完成。」 |
| 1:05 | Point at S1 = 36.5 h (+8.5 h) and S3 = #A-311 → 9 號機. | 「36.5 小時是 Arrhenius 公式算的，換模是 OR-Tools 求解器排的 —— 物理和排程數字從不讓 AI 猜。下一模 #A-311 移到 9 號機，夜班缺 1 人，交期全部滿足。」 |
| 1:20 | [02:14:26] Phone buzzes. Show the phone card (R3, 第一輪核准). Pick **B 從 2 線調 1 人**, tap **核准**. | 「寫入動作要過動作把關：白名單、四級風險、格式、模擬可行。這是 R3，必須主管核准。主管在手機上按一下，決定回到狀態機，寫進稽核日誌。」 |
| 1:35 | Click **60x**. | 「狀態機說好 30 分鐘內要回到 121°C 以上，驗證是程式做的，不是 AI。」 |
| 1:50 | [02:28:50] Safety core line: 119.9°C < 120°C → 扣模. | 「掉破 120 的那一刻，安全核心直接處置。它是確定性規則，LLM 不能呼叫、不能推翻，也不等 LLM。」 |
| 2:00 | [02:31:00] Timeline: 驗證 119.5°C → H1 不成立 → H2. | 「預期沒達成 → H1 被推翻。指揮官換假設 H2：持續洩壓或感測器漂移，對照同一條蒸汽管的 6、8 號機，查 SOP〈硫化中斷處置〉→ 7 號機停用、重排。」 |
| 2:15 | [02:31:22] Phone card 「7 號機停用，重排中」. Pick **A 夜班加班**, tap **核准** (the reason is filled in for you; tap 修改 instead to type your own). | 「這次主管不同意跨組調人，選夜班加班。決定和理由都進稽核日誌，系統照主管的決定執行，排程舊版保留，可一鍵回復。」 |
| 2:30 | Pull the WAN cable (Spark) — or click **WAN 斷**, then **LAN 斷** on the Mac. | 「拔掉對外網路：模型、知識庫、求解器全在這台 DGX Spark 上，戰情室照常運作，手機走廠內 LAN。連 LAN 都斷了，現場觸控螢幕加警示燈接手。」 |
| 2:45 | Click 排程與人力 tab: Gantt v3, press 7 out, #A-311 on 9. | 「這就是重排後的排程。偵測 2 小時變 1 分鐘；中大胎交期 13.3 週降到 9.3 週。全部在一台 240 W 的邊緣主機上。」 |
| 3:00 | End on the dashboard. | 「謝謝。」 |

## If something goes wrong

- No approval card after 30 s at 10x: click 7 號硫化機溫降 again. Loading a scenario starts it fresh with a new schedule store.
- Real model slow or down: with `LLM_FALLBACK_TO_MOCK` on (the default) the commander falls back to the rule-based plan and the audit log records it. The demo still runs end to end; say so honestly. For a guaranteed run, start in mock mode (`./start.sh`).
- Phone says PIN wrong: use the on-box screen (127.0.0.1) to approve; it needs no PIN.
- Page-2 backup demo: 展示 → 大開煉機過熱 (p.2). A-BIGOPEN 112°C rising, ✓ 已自動 通知維修冷卻水路, ☐ 待核准 放慢生料投入, 排程影響 「無，大胎 #07 仍 17:00 入硫化機」.
