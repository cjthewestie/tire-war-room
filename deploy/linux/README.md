# Linux／WSL 開發環境（joy/linux-dev）

這個資料夾讓戰情室能在 Linux 上直接跑，不用 Docker。用途有兩個：Windows 筆電的 WSL2 開發環境，以及 DGX Spark 主機上不經 Docker 的備用路徑。Mac 照舊用 `./start.sh`，這裡的東西不影響 Mac。

## 檔案

| 檔案 | 做什麼 |
|---|---|
| `bootstrap_wsl.sh` | 新機器只跑一次：裝 Node 22、uv，然後執行 `dev.sh setup` |
| `dev.sh` | 日常指令：setup、app、test、ollama、pull、env、stop、watch |
| `watch_audit.py` | 把 `data/runtime/audit.jsonl` 翻成一行一行的中文：誰在什麼時候做了什麼、關鍵數字、負責的程式 |

## 怎麼跑

```bash
bash deploy/linux/bootstrap_wsl.sh      # 新機器第一次：Node 22、uv、Python 套件、前端、SOP 索引
bash deploy/linux/dev.sh app --mock     # 規則指揮官，不需要模型；開 http://localhost:8000 和 /phone
bash deploy/linux/dev.sh test           # 全套 pytest 在背景跑，log 在 .scratch/baseline/
bash deploy/linux/dev.sh ollama         # 專案自己的 Ollama：127.0.0.1:11435、context 32k
bash deploy/linux/dev.sh pull           # 拉 MODEL（預設 nemotron-3-nano:4b，8 GB 顯卡放得下）
bash deploy/linux/dev.sh app            # 改用 LLM 指揮官（LLM_MODE=llm）
bash deploy/linux/dev.sh watch          # 跟著看稽核日誌（另開一個終端機）
```

## 注意

- Windows 上用 Git for Windows clone 的 repo，`.sh` 會變成 CRLF（`core.autocrlf=true`），在 WSL 會直接失敗。要在 WSL 裡用 WSL 的 git clone。
- 專案的 Ollama 跑在 11435，不動系統的 11434。指揮官的 prompt 約 5.5k tokens，context 太小時 Ollama 會默默截斷，所以固定開 32k。
- 4B 只是 8 GB 顯卡上的替身，正式模型在 Spark 上。
- 這個分支停在 2026-10-19 的 main，`web/` 還是 npm。現在的 main 在併入 3D 工廠時改用 pnpm，要跑 main 請到 `~/tire-war-room` 用 `./start.sh`，第一次先 `sudo corepack enable`。

## 進度（2026-10-19，Joy，B 機 WSL）

- 已完成：裝 Node 22 → 在 WSL clone → `dev.sh setup`（SOP 索引 `mode=vector`、`api check` 無降級）→ 用 mock 照 p.11 走一遍（稽核日誌 90 筆，指揮官 3 次、工具呼叫 17 次）。
- 下一步：全套測試當基準 → 接 Ollama 4B 跑 p.11 → 把模型設定集中到一個 env 檔，啟動時比對 `/v1/models`，退回規則指揮官時畫面要看得到。

## 進度（2026-10-20，Joy，B 機 WSL）

- main 快轉到 `04d8f66`，已經包含 3D 工廠、F2 的修正、natkit 測試、Agent 對話台。這個分支沒有跟 main 合併。
- 用 main 接專案 Ollama 的 4B。專案 Ollama 先用本資料夾的 `dev.sh ollama` 開在 11435，然後：

  ```bash
  cd ~/tire-war-room && WARROOM_RESUME=0 MODEL=nemotron-3-nano:4b LLM_TIMEOUT_S=120 ./start.sh ollama
  ```

  `./start.sh ollama` 不帶 `MODEL` 時會用 Mac 上的 Lightning（23 GB）。上一次沒有正常關掉會留下 `running.json`，`WARROOM_RESUME=0` 讓它從頭開始。系統的 11434 沒設 context，依顯存預設只有 4096，會截掉指揮官提示，不要用。
- 4B 在 RTX 3060 Ti 上的實測：p.11 事件指揮官跑得完（無畫面 62 秒、事件結案），讀 5.5k tokens 的提示約 1.6 秒，輸出每秒約 115 tokens。問戰情室的正確率很低，見 F3；事件指揮官的兩個問題見 F4、F5。
- 下一步：在本機實驗分支把 F3–F5 的修法實際做出來、用固定題庫量改前改後，再帶著數字和 patch 開 issue 給 CJ（`warroom/` 歸 CJ）。另外試做「4B 降級模式」：Spark 上的大模型忙或掛掉時改用 4B，而不是直接退回規則。

## 發現的問題

### F1 第一輪排程跟 p.11 不一致

B 機 WSL、mock、press7_temp_drop、10×。第一輪核准卡寫「S4 排程 v2 生效（7 號硫化 36.5 h；無機台異動）」，CP-SAT 把 #A-311 留在 7 號機，入模從 10/04 04:30 延到 10/05 11:30（`audit.jsonl` #27）。企劃書 p.11 和 `tests/test_agent_page11.py:127` 預期的是「#A-311 移到 9 號機」，這個結果到第二輪才出現。推測原因：CP-SAT 有 10 秒時間上限（`warroom/schedule/solver.py:969`），即使固定了 seed，結果仍可能隨 CPU 速度不同；或者開機時的種子排程（`warroom/api/runtime.py:325-341`）在這台機器算出不同的 v1。換到 Spark 可能又不一樣，會影響展示劇情。2026-10-19 在公司筆電確認，原因是 CP-SAT 每次求解的結果不固定（8 個 worker 加上牆鐘 10 秒上限），跟 CPU 快慢無關。2026-10-20 用 4B 跑 p.11，第 1 輪又出現一次。修法待和 CJ 討論。

### F2 核准卡過期之後仍然可以核准

同一次執行。第一輪卡片的 `expires_at` 是工廠時間 02:24:25，02:28:25 按核准仍然生效（`audit.jsonl` #33、#34）。等待逾時用的是牆鐘（`warroom/agent/approvals.py:130`），卡上的到期時間用工廠時間（`warroom/agent/state_machine.py:1675`），SIM_SPEED 大於 1 時兩者就會錯開。CJ 已修（issue #6、PR #7），2026-10-19 併進 main。

### F3 問戰情室用 4B 時，工具名稱會拼錯或自己編，資料查到了卻回「查不到」

2026-10-20，B 機、main `04d8f66`、`nemotron-3-nano:4b`（Ollama 11435）、情境 normal。實際問了 3 題（7 號機現況、9 號機下一模、今晚夜班缺誰），沒有一題完整答對。模型把 `read_sensors` 拼成 `read_ensors`，還編了 `read_operators`、`read_shifts`，全部被攔下。9 號機那題的工具其實查到了「#M-201，10/03 22:30 入模」，模型仍回「查不到」（main 的 `audit.jsonl` #121–#155）。

原因有三。第一，`AskStep.tool` 刻意用自由字串（`warroom/contracts.py:1068-1072`），事件指揮官的 `PlanStep.tool` 則是 `ToolName` 列舉，所以只有問答這條路會拼錯。第二，程式只檢查回答裡的數字（`warroom/agent/ask.py:192`），「查不到」沒有數字，錯的回答照樣送出。第三，回答後面的「已攔下 N 個不允許的動作：查詢只能讀，不能變更」（`ask.py:321-322`）不分原因，名稱打錯也被說成想變更。

8 題的對照實驗：規劃格式的工具名稱改成 5 個唯讀工具的固定選項後，編造的名稱從 9 個變 0 個。給它正確的資料時，現行的中文回答提示仍有 7/8 回「查不到」；改成英文提示加 1 個範例是 6/8；再加上「有資料卻說查不到就改用範本」的程式補漏是 8/8。

### F4 4B 寫事件計畫時會撞到 1200 tokens 的輸出上限

同一台、同一個模型，用 `python -m warroom.api demo --scenario press7_temp_drop --auto-approve` 跑 p.11。指揮官呼叫 3 次，第 2 次輸出剛好 1200 tokens，JSON 被截斷（`Expecting ',' delimiter`），退回規則指揮官；第 1 次也用了 1186 tokens。上限是 `OpenAICommander` 的預設值（`warroom/agent/commander.py:293`）。4B 一份計畫光輸出就要 10 秒左右，提示讀入只要 1.6 秒，所以調高上限之外，精簡計畫格式也能直接降延遲。

### F5 4B 會寫出不存在的 `$S` 引用欄位，多出一輪核准

同一次執行。第 1 輪的 S5 `set_cure_time` 寫成 `$S1.total_extra_h`，但 cure_formula 的結果只有 `total_cure_h` 和 `extra_h`。動作把關以「args contain an unresolved $S reference」擋了兩次（`warroom/agent/gate.py:291`），驗證判定動作未完成，多一次重規劃，變成 3 輪核准（p.11 是 2 輪）。第 3 輪寫對了（36.51 h），代表把關、驗證、重規劃會自己修回來，但展示劇情會跟 p.11 不一樣。指揮官提示的規則 9 已經讓程式代填 `cure_overrides_h`（`warroom/agent/commander.py:146`），`set_cure_time` 的時數也可以比照。
