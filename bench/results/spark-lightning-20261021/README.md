# 用 Lightning 在 DGX Spark 上實測 agent 層（2026-10-21）

給 CJ：在大會的 DGX Spark（`aitopatom-3ef3`）上，用真的 Lightning 量了速度，也錄下每一次 LLM 呼叫的完整內容（system prompt、context、token、時間），對象是你目前的主 agent、指揮官和 p.11。這份文件列出數字、行為和建議；原始資料都在這個資料夾。**這個 PR 沒有改任何程式**，只新增結果檔。

## 先講結論

1. **Lightning 本身夠快**：單流解碼 126 tok/s（p.10 寫 95–108）。第一張告警卡 p90 是 2.11 s，比目標 2.0 s 多一點。32k 熱快取首字 1.63 s，3 件同時最慢的一件 6.54 s，這兩項都沒過。
2. **主 agent 答對的題目，常常要靠程式導正**：14 輪對話裡有 7 輪是你的程式出手，例如「這要看記憶檔」「主管是要你做」「工具已經查完了」。模型第一次答錯，被程式導回正確的檔案後才答對。路由器認得出的題目，表現很穩；認不出的題目，就會答非所問、出現括號舞台動作、漏出內部欄位名。
3. **p.11 在 `afb2553` 上用 Lightning 跑了 4 次，4 次都停在安全中止**：H1 被推翻之後，模型提出的新假設還是用「CURE-07 ≥ 121°C」當預期，`_repeats_rejected` 判它重提 H1，所以中止。Demo 腳本 2:00「換 H2、7 號停用」那一段，在這版上一次都沒出現過。
4. **你部署在 Spark 8093 的新版主 agent（`memory_files.py`、`today.py`、`memory_read`、`recall_today`）沒有在 GitHub 的任何分支上**。這份報告第 2 節量的就是這一版。請推上來，不然重灌或換機器就沒有了。

## 環境

| 項目 | 值 |
|---|---|
| 主機 | DGX Spark（GIGABYTE AI TOP ATOM），121 GB 統一記憶體 |
| 模型伺服器 | 容器 `westie-llm`：vLLM 0.27.1、`--served-model-name lightning`、NVFP4、DSpark 推測解碼 3 token、`--max-model-len 65536`、`--gpu-memory-utilization 0.4`、`--kv-cache-dtype fp8`、`--enable-prefix-caching`（Mamba `align` 模式，block 4240 tokens）、`--reasoning-parser nemotron_v3`、`--tool-call-parser qwen3_coder` |
| 主 agent（第 2 節） | 8093 那一份（`~/tire-war-room-agent`，10-21 15:33 部署）原封不動複製到 `~/twr-audit-cjagent`，起在 8099；比對過兩邊檔案相同，用它自己的 `.venv` |
| p.11（第 3 節） | main + #83，也就是 `afb2553` |
| 重現測試（第 4 節） | `afb2553`、`999c006`（#58） |
| 記錄方式 | `llm_trace_proxy.py` 放在 app 和 vLLM 中間，只把 app 的 `BASE_URL` 改成 `http://127.0.0.1:8198/v1`，其他都不動。代理會把請求原封不動轉給 vLLM，回應結束後才用 vLLM `/tokenize` 算 token，不會拖慢 app；也不記任何 request header，所以不會記到 API 金鑰 |

同一時間 Spark 上還有其他人的戰情室實例（8090–8097）在用同一個 `westie-llm`。bench 起跑前確認過 vLLM 上沒有其他請求；主 agent 實測時，有幾次撞到 `Running: 2 reqs`。

## 1. 速度（bench，直接打 westie-llm）

`bench/py.sh -m bench.decode --runs 10`、`bench.ttft --runs 5`、`bench.concurrent --runs 3`，參數 `BASE_URL=http://127.0.0.1:8000/v1 MODEL=lightning`。原始結果在 `bench/`。

| 指標 | 目標 | Spark 實測 | 判定 |
|---|---|---|---|
| 單流解碼 p50（指揮官完整計畫串流，n=10） | ≥ 95 tok/s | **126.4 tok/s** | 過，比 p.10 寫的 95–108 還快 |
| 第一張告警卡 p90（冷，prompt 約 6.9k，n=5） | ≤ 2.0 s | **2.11 s** | 差一點沒過 |
| 第一張告警卡 p90（同一個請求重複一次） | 參考 | 2.47 s | 沒變快，app 尺寸的 prompt 沒吃到 prefix cache |
| 32k 首字，熱快取，p90 | ≤ 0.5 s | **1.63 s** | 沒過（Mac 上是 4.51 s） |
| 32k 首字，冷，p50 | 參考 | 6.71 s | — |
| 3 件同時，最慢的一件 | ≤ 4.0 s | **6.54 s**（p50 5.26 s；單獨跑一件 1.97–2.47 s） | 沒過；3 輪都有被批次處理 |

**prefix cache**：從開機到 bench 前，`prefix_cache_hits_total` 一直是 0（查詢超過 226 萬次）。bench 跑完後變成 127,200 tokens，等於 4240 × 30 個 block，全部來自 32k 那組。Mamba `align` 模式下，一個 block 是 4240 tokens，只有很長的相同前綴才吃得到快取。戰情室的 prompt 大約 3–7k tokens，到現在 0 命中。簡報如果要講「prefix cache 加速」，只能拿 32k 長文來講。

**app 實際跑出來的速度跟 bench 一致**：4 次 p.11，指揮官一共呼叫 11 次，首字 1.13–1.32 s、解碼約 123–137 tok/s、每次 9.2–11.4 s（見 `p11/summary.txt`）。

## 2. 主 agent（8093 版）實測：14 輪、41 次呼叫

Joy 在 8099 上問，原始紀錄在 `llm-trace.jsonl`，一行一次呼叫，內容包括 system prompt 全文、每一則 message、模型輸出、tool calls、token 數和時間。每一輪的彙總在 `turns.json`。

### 每次呼叫

| 項目 | 主 agent（41 次） | 指揮官（1 次，子 agent） |
|---|---|---|
| 呼叫方式 | 非串流、tool calling、8 個工具、`max_tokens=500`、`temperature=0`、`enable_thinking=false` | 串流、輸出 JSON、`max_tokens=4096` |
| system prompt | **1,621 tok**（每次都一樣） | **4,897 tok** |
| 輸入 | 2,608–4,965 tok，中位數 3,422 | 5,673 tok |
| 輸出 | 26–192 tok，中位數 36 | 586 tok |
| 每次耗時 | 0.85–4.66 s，中位數 1.39 s | 首字 1.16 s、整次 8.10 s、84 tok/s |
| finish_reason | 17 次 `tool_calls`、24 次 `stop`，**0 次 `length`** | `stop` |
| 退回規則 | 0 次 | 0 次 |

- 8 個工具的定義大約佔 1,150 tok：最後一圈拿掉工具以後，輸入從 3,760 降到 2,608。
- 主 agent 非串流呼叫，扣掉讀入 prompt 的時間後，產生速度大約只有 40–50 tok/s，指揮官的串流是 84–137 tok/s。原因還沒拆開：可能是非串流加上 `qwen3_coder` 工具解析，也可能是輸出太短、推測解碼幫不上忙，或者剛好跟別人同時在跑。

### 每一輪

「導正」是指你的程式插進一則 user 訊息去推模型（`main_agent.py` 的 `wants_memory` 路由和後面的提醒）。

| 主管的話 | 呼叫次數 | 總時間 | 導正 | 結果 |
|---|---|---|---|---|
| SO-004 什摸時候完成 | 4 | 5.1 s | 「這要看記憶檔」→ orders.md | ✅ 第 2 圈先答「排在 CURE-03、今天下午 3 點出模」（錯），導正後答「11-05 09:00 前完成」（對） |
| 之前大開煉機出過什麼事？（問 2 次） | 2／2 | 2.4／2.3 s | — | ✅ 兩次輸入完全相同，回答略有差異（溫度 0 也不保證一字不差） |
| 今天排程改了幾版？ | 2 | 1.9 s | — | ✅ |
| 現在有什麼在等我核准？ | **7** | **8.2 s** | 撞到 `MAX_STEPS` →「工具已經查完了」 | ⚠️ 答案對，但 `recall_today` 換了 6 個關鍵字（`等核准`、`waiting_approval`、`constraints`、`open`、`decisions_today`、`closed_today`），每次都回 0 筆；回答又漏出 `waiting_approval／open／constraints` |
| 我筆記寫了什麼？ | 3 | 7.4 s | 「這要看記憶檔」→ notes.md | ✅ 第 1 圈沒用工具就直接答了一段自我介紹（192 tok、4.66 s），導正後才答對 |
| 哈摟 | 1 | 2.7 s | — | ✅ |
| 有啥要處理的 | 1 | 2.1 s | — | ⚠️ 答案對，但回答寫了「open 為空」「constraints 為空」 |
| 不市一個 代理一個畫面媽（問 2 次） | 3／3 | 8.9／7.2 s | 「這要看記憶檔」→ about.md | ❌ 兩次都沒猜到是「不是一個代理一個畫面嗎」：一次答「你是說要我畫個畫面嗎？」，一次猜成「不如有一個…」。兩次都出現「（看著訊息，稍微歪頭）」「（點點頭）」這種括號舞台動作 |
| #M-201 可能晚交，請重排保交期 | 4 | 6.6 s | 「主管是要你做這件事」→ `propose_plan` | ⚠️ 第 1 圈讀了 about.md；主 agent 回「已擬計劃送您核准」，可是子 agent 求解後排程沒變，**沒有出核准卡**，25 秒後就以「沒有可執行的動作（主管指令為 supervisor_command…）」結案；主管會以為有一張卡在等 |
| 看一下目前有哪些風險 | 3 | 5.3 s | — | ✅ 自己查了感測器和訂單班表，也把剛才的 #M-201 列為待關注 |
| 交期預警呢 | 3 | 5.1 s | — | ⚠️ 數字有根據，但寫「兩筆入模排程」卻列了三筆；只講 #M-201 |
| 狀態機頁在看什麼？ | 3 | 5.1 s | 「這要看記憶檔」→ about.md | ❌ 讀了 about.md 還是在講今天的狀態；8093 稍早答同一題是對的 |

合計：✅ 7 輪、⚠️ 4 輪、❌ 3 輪。導正 7 次，導正後答對或大致可用的有 4 次，3 次導正後仍然答錯。

### 從 trace 看到的 harness 行為

- **路由器是關鍵**：`wants_memory()` 認得的題目，例如訂單、事故紀錄、筆記、說明文件，就算模型第一次答錯，也會被拉回正確的檔案。
- **`earlier_messages` 是選擇性帶入的**：只有追問（短句、像「那 8 號呢」、或上一輪模型反問過）才會帶，獨立的問題是空的，換日也會清掉。這個設計是對的。不過「#M-201 可能晚交」不是追問，卻也帶進了前面的問題。
- **重複呼叫沒擋住**：#58 盤點時提過，去重是看「工具＋參數」，參數一換就擋不住。這次在 8093 版上用 Lightning 重現了（「等核准」那一輪）。
- **context 裡本來就有答案**：待核准的卡片已經在 `context.today`／`waiting_plan` 裡，不需要用 `recall_today` 去猜關鍵字。

## 3. p.11（`afb2553`，Lightning，4 次）

驅動方式：載入 `press7_temp_drop`（preroll，10×），每張卡都選建議的方案核准（第二輪選 B）。逐筆稽核在 `p11/*-audit.jsonl`，路徑摘要在 `p11/summary.txt`。

| 次 | 第一輪 | 第二輪 | 第三次規劃 | 結束 |
|---|---|---|---|---|
| 1 | 延長硫化被把關擋下（同一事件沒有可行的 28.5 h 重排），卡片上只有「排程 v2（無機台異動）」；核准後判「動作未完成」 | H1，延長到 28.5 h，核准；溫度掉到 119.5 → H1 不成立 | `steam_pressure_low`，預期還是 ≥121 | `commander re-proposed a rejected hypothesis` → safe_stop |
| 2 | 同上 | 同上 | `steam_pressure_low` | safe_stop |
| 3 | 只出一張「通知維修」卡 | H1，28.5 h | `steam_pressure_low` | safe_stop |
| 4 | 28.5 h、#A-311 改 5 號 | — | `steam_pressure_low_or_sensor_drift`，預期還是 ≥121 | safe_stop |

- `_repeats_rejected`（`state_machine.py:1366-1377`）比的是同機台、同感測值、同比較方向、門檻差 0.5 以內，判定本身沒錯。問題在 Lightning 在新資料集的劇情下，換不掉「回溫到 121」這個驗證方式。
- 第一輪如果有寫入步驟被把關擋下，核准卡上不會說，主管核准後才判「動作未完成」，多用掉一輪。
- 卡片寫「#A-311 改 5 號」，`docs/DEMO-SCRIPT.md` 寫「改 6 號」。
- 同一天早上在 `bb1e0e0` 上跑了 1 次，成功換到 H2。樣本太少，還不能說是資料集那串合併造成的退化。
- 你自己的 agent 測試在 `afb2553` 上 186 個全過（mock 指揮官）：真模型的行為，這些測試蓋不到。

## 4. 重現測試（`tests_audit/`，mock，不用 GPU）

放在這裡、不放進 `tests/`，因為現在在 main 上都是紅的。`pytest` 的 `testpaths` 只收 `tests/`，所以不會影響現有測試。執行：`python -m pytest bench/results/spark-lightning-20261021/tests_audit -q`。

| 測試 | 問題 | 在 `afb2553`、`999c006` 上 |
|---|---|---|
| `test_n9_orphan_approval.py`（2 支） | 事件停在 AWAIT_APPROVAL 時被取消或丟例外，核准卡一直是 PENDING；`_drive` 的 finally 沒處理 `run.waiting_req`（`state_machine.py:347-359`） | 紅 |
| `test_n1_stream_timeout.py`（2 支） | 串流時 `LLM_TIMEOUT_S` 只限兩段資料之間的空檔，不限整次呼叫：每秒吐一段、timeout 2 s，整次 8 s 照樣成功（`commander.py:306-307`、`:342-351`）；對照組空檔 3 s 會逾時 | 紅 1、綠 1（對照組） |
| `test_n3_empty_steps_stable.py`（2 支） | 指揮官交空步驟，或只交讀算步驟，而溫度穩在 122°C 時，事件以「H1 CONFIRMED、無需動作」結案，`set_cure_time` 一次都沒呼叫（`state_machine.py:970-971`、`:998-1001`、`:3033`）。需要你確認這是不是刻意的設計 | 紅 |

## 5. 其他實測發現

- **問戰情室的硫化時數是模型編的**：問「7 號機現在要硫化多久？」，`cure_formula` 的 `actual_temp_c` 由模型填，兩次都填 165°C（實際 128°C），答案也不同（3.2 h、3.7 h），卻都通過了數字檢查（`ask.py:53`、`:258`、`:221-227`）。主 agent 問「溫度掉下來怎麼處理」，也自己填了 118°C。建議比照狀態機的 `_bind_physics`，由程式填這兩個值。
- **數字檢查把正確答案換成範本句**：問「哪幾台能做大胎？」，模型答 8、9、11，因為這些數字只出現在 prompt 裡的 PLANT 事實，被判成沒有根據，整段換成無關的範本句，裡面還有「多 -28 h」（`ask.py:272`、`:221-227`）。
- **哪幾台做大胎，各處講法不一**：`commander.py:132` 寫 CURE-08..11，`config/plant/PLANT.md:10`、`ask.py:59` 和三份 SOP 寫 7–10 號，正本在 `factory.py:68`。
- **兩份訂單資料不一致**：模擬器訂單簿的 SO-004 是 19 條、交期 10-15；`data/team_dataset/orders.csv` 是 20 條、交期 11-09。主 agent 讀資料集，問戰情室和指揮官讀模擬器，同一張單會講出兩套數字。
- **健康檢查假綠燈**：金鑰錯（401）或網址少 `/v1`（404）時，`/api/health` 仍然回 `llm_reachable: true`（`runtime.py` 的 `r.status_code < 500`）。
- **從 LAN 連的時候，PIN 錯了很容易誤會成「模型沒在跑」**：`/api/ask`、`/api/main/message`、`/api/sim/control` 都要 `X-Supervisor-Pin`，畫面上只顯示「查詢失敗：401」。
- **口吻**：建議在主 agent 的 system prompt 核心規則裡明講：不要括號舞台動作、不要說出內部欄位名、遇到錯字先修正再理解。現在「主管的字可能有錯字」只出現在讀記憶檔後的提醒裡。

## 6. 建議的順序

1. 把 8093 那版主 agent 推上 GitHub，開 PR。
2. p.11：第一輪有寫入步驟被擋時要寫在卡上；`_repeats_rejected` 判定重提時，先帶著「H1 的預期已被推翻，請換驗證方式或請求停用」再問一次，重規劃次數還有剩，不要直接中止。修好後用 Lightning 多跑幾次確認。
3. 問戰情室的 `cure_formula` 輸入由程式填（第 5 節第 1 點）。
4. N9：在 `_drive` 的 finally 作廢 `run.waiting_req`。
5. 主 agent：重複呼叫的判斷改看工具名稱就好，或者工具回 0 筆就直接收尾；待核准的卡直接從 context 回答；`propose_plan` 回來前，不要先跟主管說「已送核准」。
6. 統一「哪幾台做大胎」和訂單的資料來源。

## 檔案

| 檔案 | 內容 |
|---|---|
| `llm-trace.jsonl` | 主 agent 41 次加指揮官 1 次呼叫的完整紀錄（id 3–44）；欄位說明見 `llm_trace_proxy.py` 的 `new_rec` |
| `turns.json` | 依「主管的一句話」彙總：呼叫次數、總時間、token、導正、用到的工具、最終回答 |
| `bench/*.json` | decode、ttft、concurrent 的原始結果 |
| `p11/` | 4 次 p.11 的稽核切片、驅動紀錄和路徑摘要 |
| `tests_audit/` | 第 4 節的重現測試 |
| `llm_trace_proxy.py` | 記錄代理，附即時網頁 `/dash`。重現方式：在 Spark 上 `python llm_trace_proxy.py`（監聽 :8198），app 用 `BASE_URL=http://127.0.0.1:8198/v1` 啟動 |

所有檔案都檢查過，沒有 API 金鑰、主管 PIN 或 Authorization header。
