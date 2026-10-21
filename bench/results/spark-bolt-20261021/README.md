# Bolt 對 Lightning：同一套主 agent 在 DGX Spark 上的對照（2026-10-21）

`spark-lightning-20261021/` 的姊妹篇。同一台 Spark、同一份主 agent（8093 版，複製到 8099），把模型從 Lightning 換成 Bolt，再跑一次同樣的 12 句固定腳本。兩邊都從乾淨的狀態開始，每一次 LLM 呼叫都經過 `../spark-lightning-20261021/llm_trace_proxy.py` 記錄。**沒有改任何程式。**

## 結論

1. **Bolt 答得比較好**：12 句裡，Bolt ✅ 11、❌ 1；Lightning ✅ 7、⚠️ 3、❌ 2。Bolt 會一次並行呼叫好幾個工具，答案短而具體，沒有括號舞台動作，也沒有漏出內部欄位名。
2. **Bolt 比較慢**：每次呼叫的中位數是 3.29 s，Lightning 是 1.12 s，大約 3 倍。不過 Bolt 用的圈數比較少（26 次對 36 次），所以 12 句的總時間是 84.9 s 對 49.8 s，約 1.7 倍。
3. **慢在讀 prompt，不在產生 token**：Bolt 當指揮官（串流）時，首字 3.48–3.95 s（Lightning 1.12–1.23 s），解碼 84–89 tok/s（Lightning 110–118 tok/s）。app 的 prompt 都短於 prefix cache 的 4,240 tok 一個 block，每一圈都得重讀 3–5k tok。**光首字就超過「第一張告警卡 ≤ 2.0 s」的目標。**
4. **程式導正還是需要**：Bolt 被導正 4 次，Lightning 5 次。說明類的問題（about.md）兩個模型都要靠 `wants_memory` 的提醒才會去讀。

## 設定

| | Lightning | Bolt |
|---|---|---|
| 模型 | Nemotron 3.5 Lightning 30B-A3B NVFP4 + DSpark（3 token） | Nemotron 3.5 Bolt 74B-A9B 24GB + LiLiCorr dflash（7 token） |
| 容器 | `westie-llm`，vLLM 0.27.1，`--gpu-memory-utilization 0.4` | `westie-bolt`（映像 `westie/bolt-ready:1016`，在隨身碟 `images/`），`--gpu-memory-utilization 0.45`，`--max-num-seqs 1`，65536 |
| 啟動方式 | 原本就在跑 | 隨身碟 `spark-test-1016/start_bolt_offline.sh`，改成 `NAME=westie-bolt PORT=8001 NONET=0`（預設會 `docker rm -f westie-llm`，要小心），就緒 271 s |
| app 送出的參數 | 主 agent 非串流、8 個工具、`max_tokens=500`、`temperature=0`、`enable_thinking=false`；指揮官串流、`max_tokens=4096` | 相同（`reasoning_tokens` 0） |

量測期間 Lightning 暫停約 14 分鐘（20:09–20:23），8090–8097 這段時間連不到模型。之後已經恢復，`/v1/models` 回 `lightning`。

## 每次呼叫

固定腳本：Lightning trace #45–#82，Bolt #98–#124。

| | Lightning 主 agent | Bolt 主 agent | Lightning 指揮官 | Bolt 指揮官 |
|---|---|---|---|---|
| 呼叫次數 | 36 | 26 | 2 | 1 |
| 輸入 tok（中位數，範圍） | 3,411（3,185–4,965） | 3,341（3,185–4,965） | 5,710–6,156 | 5,610 |
| 輸出 tok（中位數，範圍） | 36（17–202） | 55（11–189） | 582–682 | 725 |
| 每次耗時（中位數，範圍） | **1.12 s**（0.84–3.10） | **3.29 s**（2.19–5.08） | 6.4–7.0 s | 12.1 s |
| 首字 | — | — | **1.12–1.23 s** | **3.48 s** |
| 解碼 | — | — | 110–118 tok/s | 84 tok/s |

Joy 手動問的 Bolt（#83–#97，6 輪）：主 agent 每次耗時中位數 4.07 s，指揮官首字 3.95 s、解碼 88.8 tok/s。

## 每一題（固定腳本，兩邊同樣從乾淨的狀態開始）

| 主管的話 | Lightning：呼叫／秒／導正 | Bolt：呼叫／秒／導正 | 差在哪 |
|---|---|---|---|
| SO-004 什摸時候完成 | 4／4.7／1 ✅ | **2**／5.9／**0** ✅ | Bolt 第一次就讀對 orders.md |
| 之前大開煉機出過什麼事？ | 2／2.3 ✅ | 2／7.0 ✅ | Bolt 還補上了 A04 的事故 |
| 今天排程改了幾版？ | 2／1.9 ✅ | 2／5.4 ✅ | 一樣 |
| 現在有什麼在等我核准？ | **6**／5.8 ⚠️ | **1**／2.2 ✅ | Lightning 用 `recall_today` 繞了 3 圈；Bolt 直接看 context 回答 |
| 我筆記寫了什麼？ | 4／7.1／1 ✅ | 3／7.9／1 ✅ | 都要導正到 notes.md |
| 哈摟 | 1／1.3 ✅ | 1／2.2 ✅ | Bolt 只回「哈摟，有什麼事？」 |
| 有啥要處理的 | 3／2.9 ✅ | 1／3.2 ✅ | |
| 不市一個 代理一個畫面媽 | 3／5.2／1 ❌ | 3／9.1／1 ❌ | 都沒猜到是「不是…嗎」；Bolt 反問「你是想看哪個畫面？」，沒有舞台動作 |
| #M-201 可能晚交，請重排保交期 | 4／6.1／1 ⚠️ | 4／14.9／1 ✅ | Lightning 把中胎 #M-201 排到「7 號（小胎機）」，跟 `commander.py:132` 的規則矛盾；Bolt 沒有指定錯機台 |
| 看一下目前有哪些風險 | 2／3.8 ✅ | 2／8.2 ✅ | Bolt 連開煉機、下一模、人力都列了 |
| 交期預警呢 | 2／2.8 ⚠️ | 2／8.9 ✅ | Lightning 回答裡出現 `waiting_approval` |
| 狀態機頁在看什麼？ | 3／5.9／1 ❌ | 3／10.0／1 ✅ | 讀了 about.md 後，Lightning 還是在講今天的狀態；Bolt 答出「每個任務走到哪一步」 |
| **合計** | **36 次／49.8 s／5** | **26 次／84.9 s／4** | |

逐題的原始資料和最終回答在 `compare.json`，每次呼叫的完整內容在 `llm-trace-*.jsonl`。

## Joy 手動問的 Bolt（6 輪）

| 主管的話 | 呼叫／秒／導正 | 結果 |
|---|---|---|
| 狀態機頁在看什麼？ | 3／16.2／1 | ✅ |
| #M-201 可能晚交，請重排保交期 | 3／16.3／**0** | ✅ 自己呼叫 `propose_plan`，回答講出「提前到 04:30 入模、原時段往後挪」 |
| 不是一個代理一個畫面嗎？ | 3／11.9／1 | ⚠️ 前半段說「沒有多個畫面」（錯），後半段講到「每個任務由該任務的 agent 處理」（對） |
| 7 號機現在怎樣？ | 2／5.7／0 | ✅ |
| 核准之後誰去做？ | 3／9.4／1 | ✅ 講出 `/worker`、驗證、換假設出第二輪 |

## 沒量到的

- **bench**（`bench.decode`、`bench.ttft` 打 `:8001`，`MODEL=bolt`）：兩支都沒量到數字（`no call produced enough tokens`，n=0），原因還沒追，原始輸出在 `decode-bolt-*.json`、`ttft-bolt-*.json`。所以上面的速度改用 trace 裡指揮官的串流數字。
- **p.11、32k、3 件同時**：為了趕時間沒跑。Bolt 的 `--max-num-seqs 1` 一次只處理一個請求，3 件同時一定會排隊，要測的話得先改這個參數。

## 給決定用哪個模型的人

- 要**答得好**：Bolt 明顯比較好，尤其是需要理解的問題（狀態機頁、等核准、派工）。
- 要**快**：Lightning 每次呼叫快 3 倍，首字 1.2 s 對 3.5–4 s。Demo 時，一輪 Bolt 的主 agent 對話常常要 8–16 s。
- **兩個都沒過的**：錯字句。這要靠 prompt 或程式處理，換模型解決不了。

## 檔案

| 檔案 | 內容 |
|---|---|
| `compare.json` | 摘要統計、逐題 Lightning 對 Bolt、Bolt 手動 6 輪 |
| `llm-trace-lightning-replay.jsonl` | Lightning 固定腳本（#45–#82） |
| `llm-trace-bolt-replay.jsonl` | Bolt 固定腳本（#98–#124） |
| `llm-trace-bolt-manual.jsonl` | Joy 手動問 Bolt（#83–#97） |
| `replay-lightning.log`、`replay-bolt.log` | 固定腳本每一題的回覆與總秒數 |
| `decode-bolt-*.json`、`ttft-bolt-*.json` | 沒量到數字的 bench 原始輸出 |

所有檔案都檢查過，沒有 API 金鑰、主管 PIN 或 Authorization header。
