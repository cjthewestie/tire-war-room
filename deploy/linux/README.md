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

## 進度（2026-10-19，Joy，B 機 WSL）

- 已完成：裝 Node 22 → 在 WSL clone → `dev.sh setup`（SOP 索引 `mode=vector`、`api check` 無降級）→ 用 mock 照 p.11 走一遍（稽核日誌 90 筆，指揮官 3 次、工具呼叫 17 次）。
- 下一步：全套測試當基準 → 接 Ollama 4B 跑 p.11 → 把模型設定集中到一個 env 檔，啟動時比對 `/v1/models`，退回規則指揮官時畫面要看得到。

## 發現的問題

### F1 第一輪排程跟 p.11 不一致

B 機 WSL、mock、press7_temp_drop、10×。第一輪核准卡寫「S4 排程 v2 生效（7 號硫化 36.5 h；無機台異動）」，CP-SAT 把 #A-311 留在 7 號機，入模從 10/04 04:30 延到 10/05 11:30（`audit.jsonl` #27）。企劃書 p.11 和 `tests/test_agent_page11.py:127` 預期的是「#A-311 移到 9 號機」，這個結果到第二輪才出現。推測原因：CP-SAT 有 10 秒時間上限（`warroom/schedule/solver.py:969`），即使固定了 seed，結果仍可能隨 CPU 速度不同；或者開機時的種子排程（`warroom/api/runtime.py:325-341`）在這台機器算出不同的 v1。換到 Spark 可能又不一樣，會影響展示劇情。待全套測試確認。

### F2 核准卡過期之後仍然可以核准

同一次執行。第一輪卡片的 `expires_at` 是工廠時間 02:24:25，02:28:25 按核准仍然生效（`audit.jsonl` #33、#34）。等待逾時用的是牆鐘（`warroom/agent/approvals.py:130`），卡上的到期時間用工廠時間（`warroom/agent/state_machine.py:1675`），SIM_SPEED 大於 1 時兩者就會錯開。
