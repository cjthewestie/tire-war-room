# 團隊上手指南 · Team Westie

實心胎智慧製造戰情室的程式碼都在這個 repo。這份指南讓你在 30 分鐘內從零跑起來，並照同一套規則改程式。影片版：`docs/onboarding/team-onboarding.mp4`（約 3 分鐘；投影片與旁白稿在同一個資料夾）。

## 1. 第一次：拿到權限、裝工具（約 15 分鐘）

1. 收 GitHub 邀請信，按 **Accept invitation**。沒收到請找 CJ。
2. 安裝工具（Mac）：
   - git：`xcode-select --install`
   - uv（Python 管理）：`curl -LsSf https://astral.sh/uv/install.sh | sh`
   - Node.js 20 以上：`brew install node`
   - （選用）Ollama，跑真的 Nemotron 才需要：`brew install ollama`
3. 下載並啟動：

```bash
git clone https://github.com/cjthewestie/tire-war-room.git
cd tire-war-room
./start.sh
```

4. 打開 `http://localhost:8000`（戰情室）和 `http://localhost:8000/phone`（主管手機）。第一次會下載套件和 SOP 嵌入模型，約數百 MB。

## 2. 用真的 Nemotron（選用，約 23 GB）

```bash
deploy/mac/dev.sh pull      # 下載 Nemotron 3.5 Lightning NVFP4，只要做一次
./start.sh ollama           # 戰情室改用真模型
```

畫面上方顯示「即時・LLM 指揮官」就是真模型。第一個問題約 30 秒（載入模型），之後每題 2–3 秒。

## 3. 每天的工作流程

```
git pull → 開 issue → 開分支 → 改程式 → 跑測試 → push → 開 PR → 另一人看過 → merge
```

1. **更新**：`git switch main && git pull`
2. **開 issue**（下一節）。每件工作都要有 issue 號碼。
3. **開分支**，名字用 `座位/issue號-簡述`：

```bash
git switch -c c/12-reask-commander
```

4. **改程式**，只改自己座位的資料夾（第 5 節）。
5. **跑測試**：先跑自己模組 `uv run pytest -q tests/test_<模組>.py`，開 PR 前跑全部 `uv run pytest -q`（約 13 分鐘）。全部綠才能開 PR。
6. **commit**，格式用 Conventional Commits：`類型(範圍): 做了什麼`

```
feat(agent): 指揮官 JSON 錯誤時重問一次
fix(web): 手機頁核准卡在 LAN 斷時顯示提示
docs(demo): 更新 10 分鐘展示稿
```

   類型：`feat` 新功能、`fix` 修錯、`docs` 文件、`test` 測試、`chore` 雜項、`refactor` 重構。
7. **push 並開 PR**：

```bash
git push -u origin c/12-reask-commander
gh pr create --fill        # 或到 GitHub 網頁按 Compare & pull request
```

   PR 說明寫 `Closes #12`，merge 後 issue 會自動關掉。
8. **另一個人看過再 merge**。不要直接 push 到 `main`。

## 4. 怎麼開 issue

到 repo 的 **Issues → New issue**，選範本：

- **任務**：要做的事。寫清楚「做完的樣子」（可以檢查的條件），例如「真模型連跑 10 次第 11 頁，10 次都照順序」。
- **Bug**：壞掉的東西。寫重現步驟、看到什麼、預期什麼，附截圖。

標題格式：`[座位] 一句話`，例如 `[C] 指揮官 JSON 錯誤時重問一次`。貼上座位標籤 `seat-A`～`seat-D`，指派給自己。

也可以用指令：

```bash
gh issue create --title "[C] 指揮官 JSON 錯誤時重問一次" --label seat-C --assignee @me
```

## 5. 座位與負責的資料夾

只改自己座位的資料夾。要改別人的或共用的 `warroom/contracts.py`，先開 issue 找負責人。

| 座位 | 角色 | 負責 |
|---|---|---|
| A | 隊長・主講 | `docs/`（企劃書、展示稿）、`README.md` |
| B | 邊緣部署 | `deploy/`、`bench/` |
| C | Agent・後端 | `warroom/agent/`、`warroom/api/`、`warroom/tools/`、`warroom/schedule/`、`warroom/kb/` |
| D | 展示操作・前端 | `web/`、`docs/screenshots/` |
| 共用（改前先講） | — | `warroom/contracts.py`、`web/src/contracts.ts`（兩邊要同步）、`warroom/factory.py` |

## 6. 規範（一定要遵守）

1. **不加任何能動到機台的工具。** 沒有 PLC／致動器寫入工具，以後也不准加。寫入類工具只記錄意圖（排程版本、建議值、通知），一定經過動作把關（`warroom/agent/gate.py`）。
2. **安全核心不碰 LLM。** `warroom/safety/` 不能 import agent、tools、api 或任何 LLM 客戶端，有測試在擋。
3. **不提交秘密和執行資料。** `data/runtime/`、`.env`、金鑰、主管 PIN 都不進 git。PIN 用環境變數 `WARROOM_SUPERVISOR_PIN`。
4. **只寫量測過的數字。** 投影片、README、台上說的數字要有 `bench/results/` 的檔案，並標明是 Mac 還是 DGX Spark。
5. **企劃書為準。** `docs/proposal-final.pdf` 和程式衝突時，以企劃書為準，或開 issue 討論。
6. **`main` 永遠能跑。** 全部測試綠才 merge。merge 後如果壞了，先 revert 再修。
7. **改共用介面要兩邊一起改。** `warroom/contracts.py` 和 `web/src/contracts.ts` 一起改，`tests/test_contracts.py` 會檢查。
8. **說 NemoClaw 要精準。** 我們的程式跑在 OpenShell 沙箱（NemoClaw 底層的執行環境），不是「NemoClaw 支援的 agent」。

## 7. 常見問題

- **`./start.sh` 說要 uv 或 npm**：回第 1 節裝工具。
- **port 8000 被占用**：`lsof -i :8000` 找到程式關掉，或 `pkill -f "warroom.api serve"`。
- **網頁還是舊的**：瀏覽器按 Cmd + Shift + R 強制重新整理。
- **手機頁說 PIN 錯**：用電腦本機開的頁面核准（本機不需要 PIN）。
- **網路按鈕卡在「LAN 斷」**：展示列點「● 在線」。
- **測試有約 6 個跳過**：其中 5 個需要 Docker，沒開 Docker Desktop 會跳過，正常。

## 8. 重要文件

- 架構：`docs/ARCHITECTURE.md`
- 3 分鐘展示稿：`docs/DEMO-SCRIPT.md`
- 企劃書：`docs/proposal-final.pdf`
- 跑在 DGX Spark：`README.md` 的「在 DGX Spark 上跑」
