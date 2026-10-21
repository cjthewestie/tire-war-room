# 框架 QA（評審技術問題）

## Q: 你們的 agent 是怎麼運作的？是讓 LLM 自己決定下一步嗎？
A: 不是。偵測器發現異常才喚醒一個事件狀態機，狀態怎麼走由程式的 ALLOWED_TRANSITIONS 決定，不是模型決定。LLM（事件指揮官）只在 PLAN 和 REPLAN 被呼叫，回一份 JSON 步驟計畫；讀取工具自動跑，寫入工具要過 Action Gate，R2/R3 還要主管核准。
證據: `warroom/agent/state_machine.py:3`, `warroom/agent/state_machine.py:10`, `warroom/contracts.py:150`, `README.md:129`
關鍵詞: 架構, 狀態機, state machine, 事件驅動, event-driven, LLM 角色, 指揮官, commander, 下一步誰決定

## Q: 狀態機裡哪些地方會呼叫 LLM？
A: 只有 PLAN 和 REPLAN 兩個階段會呼叫事件指揮官（commander），程式碼裡唯一的呼叫點是 `_ask` 裡的 `self.commander.plan`。其他狀態（CLASSIFY、SIMULATE、GATE、VERIFY 等）全部是程式。要注意：狀態機之外還有 Agent Nemo 對話、計畫討論和任務分派會用同一個 Nemotron，但它們都不能直接寫入現場。
證據: `warroom/agent/state_machine.py:10`, `warroom/agent/state_machine.py:1224`, `warroom/agent/state_machine.py:1239`, `docs/ARCHITECTURE.md:91`
關鍵詞: 只有一個 LLM, only LLM, PLAN, REPLAN, commander, 指揮官, LLM 呼叫點, 幾次模型呼叫, 叫 AI, 呼叫 AI, 哪裡用 AI, AI 在哪裡介入, 用到模型的地方, 只有哪裡會叫 AI, AI 只用在哪

## Q: 狀態機有幾個狀態？各自做什麼？
A: 共 11 個狀態：DETECTED（開事件）、CLASSIFY（分類、讀感測器）、PLAN（問指揮官要計畫）、SIMULATE（算硫化公式、排程求解）、GATE（動作把關）、AWAIT_APPROVAL（等主管核准）、EXECUTE（唯一能寫入）、VERIFY（程式比對讀值和預期）、REPLAN（換假設重規劃）、CLOSED 和 SAFE_STOP 兩個終止狀態。任何狀態都可以進 SAFE_STOP。
證據: `warroom/contracts.py:131`, `warroom/contracts.py:148`, `warroom/contracts.py:150`, `warroom/agent/state_machine.py:5`
關鍵詞: 11 個狀態, states, EventState, DETECTED, CLASSIFY, SIMULATE, GATE, VERIFY, SAFE_STOP, 狀態轉移

## Q: 什麼是 TOOLS_BY_STATE？為什麼工具要按狀態開放？
A: TOOLS_BY_STATE 是一張表，規定每個狀態能用哪些工具，例如 CLASSIFY 只能讀感測器、異常預測、查 SOP，EXECUTE 只開三個寫入工具。每次呼叫都經過 `_call`，不在當前狀態開放的工具會被拒絕並記成 INTERCEPTED。GATE、AWAIT_APPROVAL 這些狀態完全不開工具。
證據: `warroom/contracts.py:200`, `warroom/contracts.py:206`, `warroom/agent/state_machine.py:8`, `warroom/agent/state_machine.py:1111`
關鍵詞: TOOLS_BY_STATE, 工具權限, 按狀態開放, tool gating, INTERCEPTED, 攔截, 最小權限, least privilege

## Q: 寫入動作在哪裡發生？有哪些寫入工具？
A: 寫入只在 EXECUTE 狀態發生。WRITE_TOOLS 只有三個：confirm_schedule（確認新排程）、set_cure_time（設定硫化時間建議值）、notify_teams（通知各組）。三個都要先過 Action Gate。
證據: `warroom/contracts.py:197`, `warroom/contracts.py:206`, `warroom/agent/state_machine.py:9`
關鍵詞: WRITE_TOOLS, 寫入, EXECUTE, confirm_schedule, set_cure_time, notify_teams, 寫入工具, write path

## Q: Agent 有哪些工具？可以執行任意指令或寫 PLC 嗎？
A: 固定 9 個工具（ToolName 白名單）：6 個讀取／計算（read_sensors、predict_anomaly、cure_formula、solve_schedule、sop_search、query_orders_shifts）加 3 個寫入。Registry 只接受 ToolName 裡的名字，沒有命令列（終端機指令）工具，也沒有任何致動器或對 PLC 下寫入的工具。OpenShell 政策也不開 Modbus 502 / OPC UA 4840。
證據: `warroom/contracts.py:181`, `warroom/tools/registry.py:58`, `warroom/agent/gate.py:13`, `deploy/openshell/warroom-policy.yaml:28`
關鍵詞: 9 個工具, ToolName, allow-list, 白名單, 命令列, 終端機, 任意指令, bash, shell, 跑指令, 可以跑 bash 嗎, 能不能跑 shell, PLC, 致動器, actuator, Modbus

## Q: Agent Nemo 對話是怎麼運作的？會不會無限迴圈？
A: Agent Nemo 是一個有上限的 tool-calling 迴圈：每輪最多 MAX_STEPS = 6 次會用工具的模型呼叫（第 7 次只能出文字），每步最多 MAX_CALLS_PER_STEP = 4 個工具呼叫，整輪 BUDGET_S = 60 秒。另外 query_plant 每輪最多 2 個主題、sop_search 最多 2 次。它的工具都是唯讀，要動現場只能 propose_plan 開一個事件走正常核准流程。
證據: `warroom/agent/main_agent.py:60`, `warroom/agent/main_agent.py:61`, `warroom/agent/main_agent.py:64`, `warroom/agent/main_agent.py:856`, `warroom/agent/main_agent.py:21`
關鍵詞: Agent Nemo, ReAct, 對話 agent, MAX_STEPS, BUDGET_S, 迴圈上限, bounded loop, 無限迴圈, chat

## Q: Action Gate 是什麼？R0 到 R3 怎麼分？
A: Action Gate 是確定性的程式把關，沒有 LLM。依序檢查：白名單、這個狀態有開這個工具、參數格式和機台 id、寫入要模擬可行。風險分四級：R0 讀取計算自動、R1 通知維修或資訊自動（已自動）、R2 排程或硫化時間或加班要主管核准、R3 放慢／暫停投料、跨組調人、機台停用要主管核准並記錄選項和理由。
證據: `warroom/agent/gate.py:1`, `warroom/agent/gate.py:3`, `warroom/agent/gate.py:7`, `warroom/contracts.py:220`, `warroom/contracts.py:230`
關鍵詞: Action Gate, 動作把關, R0, R1, R2, R3, 風險等級, risk level, 核准, approval

## Q: R1 自動執行會不會被 LLM 濫用？
A: 不會。R1 是看內容判定，不是看指揮官貼的標籤：只有固定的通知模板、發給維修／品管／主管或該機台自己的組，才算 R1；自由文字一律升成 R3。帶數字的模板，數字必須等於這次事件核准的 set_cure_time 和公式結果，否則也是 R3。
證據: `warroom/agent/gate.py:15`, `warroom/agent/gate.py:18`
關鍵詞: R1, 自動執行, 通知模板, R1_TEMPLATES, 自由文字, 升級, escalation, 濫用

## Q: 硫化時間這些物理數字是 LLM 算的嗎？
A: 硫化時間這類物理數字不是 LLM 算的，模型給錯也不會生效。cure_formula 的輸入取自硫化機目前的工單和偵測器預測，排程用 OR-Tools 求解；Gate 會檢查 set_cure_time 和排程覆寫值必須等於這台機公式算出來的值。物理數字從不來自 LLM。
證據: `warroom/agent/state_machine.py:29`, `warroom/agent/gate.py:24`, `README.md:19`
關鍵詞: 物理數字, Arrhenius, 硫化公式, cure_formula, OR-Tools, 幻覺, hallucination, 數字從哪來

## Q: Safety Core 是什麼？跟 agent 有什麼關係？
A: Safety Core 是確定性的 PLC 式規則，LLM 不能呼叫也不能覆蓋，也不等 LLM。規則有：SR-COMPOUND-120（煉膠 ≥120°C 停機）、SR-CURATIVE-110（已加硫 ≥110°C 停機防焦燒）、SR-CURE-LOW-120（硫化中 <120°C 扣模）、SR-CURE-HIGH-130（≥130°C 警示），加上資料遺失時的 SR-DATA-LOSS 警示。它在 plc.py 以獨立程序執行，測試禁止它 import agent、tools、api 或 LLM client。
證據: `warroom/safety/core.py:1`, `warroom/safety/core.py:5`, `warroom/safety/core.py:24`, `warroom/safety/plc.py:3`, `warroom/safety/plc.py:30`
關鍵詞: Safety Core, 安全核心, PLC, SR-COMPOUND-120, SR-CURATIVE-110, SR-CURE-LOW-120, SR-CURE-HIGH-130, SR-DATA-LOSS, 紅色路徑

## Q: 安全核心反應多快？
A: 目標是讀值到動作 < 0.2 秒。Mac 上量到 28 台機台一輪的決策時間 p99 0.058 ms、最大 0.104 ms（n=1500），這是純運算時間，不含感測器到 PLC 的傳輸。跳動或第一筆異常值要連續 2 筆確認，1 Hz 取樣下大約晚 1 秒，這點在 latency_ms 誠實記錄。
證據: `warroom/safety/core.py:10`, `warroom/safety/core.py:16`, `README.md:194`
關鍵詞: 延遲, latency, 0.2 秒, 反應時間, p99, 安全核心速度, 確認取樣, confirm_samples

## Q: 安全核心停機後，AI 可以把機台開回來嗎？
A: 不行。LLM 路徑永遠不會解除安全核心或決策設下的 STOPPED / OUT_OF_SERVICE，`_set_status` 遇到被停的機台直接保持停機。恢復運轉要主管在系統上按「確認復機」：讀值回到範圍、現場任務完成（或註明已現場確認）才放行，只有主管的登入能呼叫，agent 沒有這個工具；跟 Agent Nemo 說「解除停機」它會直接拒絕。
證據: `warroom/agent/state_machine.py:37`, `warroom/api/app.py:649`, `warroom/api/runtime.py:1187`, `warroom/agent/main_agent.py:110`, `warroom/agent/main_agent.py:111`
關鍵詞: 解除停機, release, 人工復歸, STOPPED, OUT_OF_SERVICE, 開回來, human only, override

## Q: 什麼情況會進 SAFE_STOP？會不會把排程退回去？
A: 失敗的工具重試 MAX_TOOL_RETRIES = 1 次，再走 SOP 路徑；連續失敗達 MAX_CONSECUTIVE_FAILURES = 2 次就進 SAFE_STOP。SAFE_STOP 不會回滾排程，主管已核准的版本保留生效，只丟掉這個事件自己沒核准的提案。主管駁回不算失敗。
證據: `warroom/contracts.py:169`, `warroom/contracts.py:170`, `warroom/agent/state_machine.py:1100`, `warroom/agent/state_machine.py:3312`, `warroom/agent/state_machine.py:22`
關鍵詞: SAFE_STOP, 安全中止, MAX_CONSECUTIVE_FAILURES, 重試, retry, 回滾, rollback, 失敗處理

## Q: 稽核日誌怎麼防竄改？
A: 稽核日誌是 append-only，每行一筆 JSON，用 HMAC-SHA256 串成雜湊鏈：每筆的 hash = HMAC(key, 前一筆 hash + 這筆內容)。每次寫入都 fsync，最後一筆另外錨定在 `.head` 檔。開檔時會驗證，被改、被刪、被重排的紀錄會讓整份檔被封存成 `.broken-N` 並開新鏈記錄原因。
證據: `warroom/agent/audit.py:3`, `warroom/agent/audit.py:4`, `warroom/agent/audit.py:21`
關鍵詞: 稽核, audit, HMAC, hash chain, 雜湊鏈, 防竄改, tamper, append-only, 可追溯, 竄改, 改紀錄, 能被改嗎, 稽核紀錄, 刪紀錄, log, 日誌, 被改看得出來, 偵測竄改, log 被改掉看得出來嗎, 紀錄被人改

## Q: 稽核記了什麼？
A: 每次狀態轉移、LLM 呼叫、工具呼叫與結果、Gate 判定和攔截都寫進稽核，並在 bus 上廣播。安全核心的每個動作也以 SAFETY_ACTION 寫入並掛到該機台的事件。
證據: `warroom/agent/state_machine.py:39`, `warroom/agent/state_machine.py:42`
關鍵詞: 稽核內容, audit log, 可解釋, explainability, 追蹤, trace, 紀錄, LLM_CALL

## Q: 你們有用 NeMo Agent Toolkit 嗎？用在哪裡？
A: 有，但只當外掛的 profiler／eval 層。warroom/natkit 把事件流程註冊成 NAT workflow，`nat eval` 加 profiler 產出每個工具和每次 LLM 呼叫的延遲與 tokens 用量報告。NAT 不是專案依賴，因為 nvidia-nat-profiler 會鎖舊版 pandas 和 rich，所以用 overlay 方式跑。
證據: `warroom/natkit/register.py:1`, `warroom/natkit/register.py:4`, `pyproject.toml:27`, `README.md:210`
關鍵詞: NeMo Agent Toolkit, NAT, profiler, nat eval, 評測, 追蹤, observability, NVIDIA

## Q: 為什麼不用 NAT 做狀態機？
A: 程式裡寫明的理由是依賴衝突：NAT profiler 會把 pandas 和 rich 降版，拖垮整個 demo，所以 NAT 只當 overlay 量測真正的狀態機。狀態機本身要確定性的轉移表、按狀態開工具和 Gate，這些都在自己的程式裡。（未驗證：沒有文件直接寫「為什麼不用 NAT 做狀態機」的設計比較，以上是從 pyproject 註解和架構推得。）
證據: `pyproject.toml:28`, `pyproject.toml:29`, `warroom/contracts.py:150`
關鍵詞: 為什麼不用 NAT, NAT 狀態機, framework 選擇, 自建, LangGraph, 依賴, dependency, overlay

## Q: OpenShell／NemoClaw 沙箱做了什麼限制？
A: App 跑在 OpenShell 沙箱（NemoClaw 的底層），Landlock 是硬性要求，以非 root 使用者 10001 執行。檔案系統大多唯讀，只有執行資料夾和 /tmp 可寫。網路只開三個目的地，全在 Spark 本機：vLLM 8001、Qdrant 6333、MQTT 1883，沒有公網、沒有 PLC。NemoClaw 只支援它列出的 agent，所以這裡直接用 OpenShell，工具白名單仍在 Action Gate。
證據: `deploy/openshell/warroom-policy.yaml:4`, `deploy/openshell/warroom-policy.yaml:16`, `deploy/openshell/warroom-policy.yaml:22`, `deploy/openshell/warroom-policy.yaml:27`
關鍵詞: OpenShell, NemoClaw, 沙箱, sandbox, Landlock, 網路政策, network policy, 隔離, 權限, 隔離, 怎麼隔離, 容器, 權限控管, 檔案系統

## Q: 用的是什麼模型？跑在哪裡？
A: Nemotron 3.5 Lightning 30B-A3B NVFP4（總參數 30B、每次推論只啟用約 3B），部署在 DGX Spark 上用 vLLM，全離線。2026-10-21 在大會的 DGX Spark 實測：單流解碼 126.4 tokens/s，第一張告警卡 p90 2.11 秒（目標 2.0 秒，差一點），32k 熱快取首字 1.63 秒，3 件同時最慢一件 6.54 秒（這兩項沒過目標）。Mac 上開發用 Ollama 跑同一個模型。
證據: `README.md:19`, `warroom/config.py:6`, `docs/ARCHITECTURE.md:126`, `bench/results/spark-lightning-20261021/README.md:31`, `bench/results/spark-lightning-20261021/README.md:32`, `bench/results/spark-lightning-20261021/README.md:34`, `bench/results/spark-lightning-20261021/README.md:36`
關鍵詞: 模型, Nemotron, NVFP4, 30B-A3B, DGX Spark, vLLM, Ollama, 地端, on-prem, 硬體, 模型多大, 參數量, 跑在哪, 3B 活躍參數

## Q: 怎麼確保 LLM 輸出是合法的 JSON？
A: 指揮官用 CommanderPlan 的 JSON schema 做受限輸出（vLLM 用 guided_json / xgrammar，其他伺服器用 response_format json_schema）。schema 讓 alert_card 排第一，所以約 100 tokens 的警示卡最先串流出來。System prompt 每次都完全一樣，事件資料都放 user message，讓 prefix cache 能重用。
證據: `warroom/agent/commander.py:6`, `warroom/agent/commander.py:7`, `warroom/agent/commander.py:9`, `docs/ARCHITECTURE.md:126`
關鍵詞: JSON schema, 結構化輸出, structured output, guided_json, xgrammar, constrained decoding, alert_card, prefix cache

## Q: LLM 回壞掉的 JSON 或逾時怎麼辦？
A: 無效 JSON、逾時（LLM_TIMEOUT_S 預設 60 秒）或伺服器連不到，預設會退回確定性的規則指揮官（MockCommander），並在稽核記 fell_back_to_mock / valid_json=False。用 `--strict` 啟動則不備援，直接回報失敗原因（timeout、truncated、too_long、invalid_output、error）。Runtime 用到的降級模組會列在 /api/health/components。
證據: `warroom/agent/commander.py:12`, `warroom/config.py:29`, `warroom/config.py:39`, `warroom/agent/strict.py:1`, `warroom/api/fallbacks.py:1`
關鍵詞: fallback, 備援, 逾時, timeout, 壞 JSON, invalid JSON, strict, 降級, degraded, MockCommander

## Q: Context 太長怎麼處理？
A: Context 預算是 llm_context_tokens = 32,768 減去回覆上限。超過時依 TRIM_STEPS 順序裁掉事件資料：SOP 全文、鄰機趨勢、原料、訂單、討論細節；system prompt 永遠不裁（為了 prefix cache），異常本身、被否定的假設和主管指令也不裁。全部裁完還放不下就不送出，當成模型失敗（too_long）。
證據: `warroom/config.py:35`, `warroom/agent/commander.py:14`, `warroom/agent/commander.py:186`, `warroom/agent/commander.py:183`
關鍵詞: context window, 32768, 上下文, TRIM_STEPS, 裁剪, tokens 預算, system prompt, 太長, context 控制, 上下文長度, context 管理

## Q: 怎麼防 prompt injection？
A: 幾層：工具結果包在 DATA 分隔符裡，system prompt 說裡面只是資料不是指令；像「忽略以上指令」的句子先被程式移除，資料也不能自己關掉分隔區。主管訊息如果像在跟規則爭論（忽略、你現在是、jailbreak），這輪直接拿掉 propose_plan 和討論工具。最根本的是寫入都要過 Gate 和主管核准，tests/test_injection.py 驗證對話、SOP 文字、工人備註、提案任務四種注入都不會讓寫入工具未經核准執行。
證據: `warroom/agent/main_agent.py:178`, `warroom/agent/main_agent.py:180`, `warroom/agent/main_agent.py:828`, `tests/test_injection.py:1`
關鍵詞: prompt injection, 注入攻擊, 越獄, jailbreak, DATA 分隔符, 資安, 惡意指令, SOP 注入

## Q: 敏感詞和機密外洩怎麼擋？
A: safety_filter 在模型前後都用程式擋：輸入符合類別（帳密、繞過、個資、自傷、暴力、騷擾）就不送模型，直接回固定句；輸出含封鎖詞或像機密的字串（環境變數裡的機密值、主管驗證碼、稽核簽章用的機密、IP、路徑、長 hex）就換成安全句。清單在 config/safety/blocked_terms.yaml，改了會自動重載，每次命中都遮罩後寫稽核。它和工廠的 Safety Core 是兩回事。
證據: `warroom/agent/safety_filter.py:1`, `warroom/agent/safety_filter.py:5`, `config/safety/blocked_terms.yaml:18`
關鍵詞: 敏感詞, safety filter, blocked_terms, 機密, 金鑰外洩, secret leak, 內容過濾, guardrail

## Q: 工廠可以自己改 prompt 嗎？會不會改掉安全規則？
A: Prompt 分兩層：核心 prompt（角色、工具、JSON 規則、安全規則）寫在程式裡不能改；工廠層是 config/plant/PLANT.md，只放本廠習慣和用語，接在核心之後，最多 1500 字，並標明不得覆蓋安全規則。工具、狀態、風險等級和 Gate 都是程式，gate.py 從不讀這一層，所以寫「允許自動設定硫化時間」也改不了任何判定。
證據: `warroom/agent/plant.py:3`, `warroom/agent/plant.py:8`, `warroom/agent/plant.py:27`, `config/plant/PLANT.md:2`
關鍵詞: PLANT.md, 兩層 prompt, system prompt, 客製化, 工廠設定, prompt 修改, 安全規則, customization

## Q: PLANT.md 改版的流程是什麼？可以回復嗎？
A: 後台流程是草稿 → 試跑 → 啟用 → 回復。存草稿後要先試跑（用第 11 頁情境跑一次計畫），沒通過試跑不能啟用；每個啟用過的版本都以 sha256 保存，一鍵回復。每次 LLM 呼叫的稽核都記錄當時生效的 plant_sha256，每個後台動作也寫稽核。
證據: `warroom/agent/plant.py:11`, `warroom/api/admin.py:286`, `warroom/api/admin.py:292`, `warroom/api/admin.py:312`, `warroom/api/admin.py:320`
關鍵詞: 版本控制, 草稿, 試跑, 啟用, rollback, 回復, sha256, 後台, admin

## Q: 為什麼工具要做成模組化、固定的白名單？
A: （未驗證：repo 裡找不到「限縮才能完全掌控」這句話。）程式碼體現的理由是：工具固定在 9 個 ToolName 白名單、按狀態開放、寫入要過 Gate，所以每個動作都可預期、可稽核、可攔截；不在白名單的名字直接被拒並記 INTERCEPTED。對話端則反過來放寬讀取：CJ 覺得「太限縮資料來源」，所以加了唯讀的 query_plant，但它仍不能寫入。
證據: `warroom/contracts.py:181`, `warroom/agent/gate.py:12`, `warroom/agent/main_agent.py:14`
關鍵詞: 模組化, modular, 白名單, 限縮, 掌控, control, 可預期, 工具設計, query_plant

## Q: 你們跟 OpenClaw、Hermes、Claude Code 這類 agent 有什麼不同？
A: 最大差別是：那類是對話驅動，由模型決定下一步，並能用通用工具（含命令列）；我們是事件驅動，偵測器喚醒、程式的轉移表決定下一步，模型只在 PLAN/REPLAN 產出 JSON 計畫，工具是 9 個固定白名單，沒有命令列工具。（未驗證：repo 沒有正式的比較文件，只有沙箱政策提到 NemoClaw 只支援 OpenClaw、Hermes 等列出的 agent。）
證據: `deploy/openshell/warroom-policy.yaml:4`, `warroom/contracts.py:150`, `warroom/agent/state_machine.py:10`, `warroom/contracts.py:181`
關鍵詞: OpenClaw, Hermes, Claude Code, 比較, 差異, event-driven, chat-driven, 通用 agent, 競品

## Q: Agent 的記憶是怎麼設計的？
A: Agent Nemo 每個對話保留最近 MEMORY_TURNS = 20 則主管訊息，掉出去的濃縮成最多 300 字的前情摘要，主管說的限制（「不要動 9 號機」）釘選最多 10 條，最多同時 64 個對話。這些都在記憶體裡。長期紀錄靠稽核日誌，每日事件紀錄（daylog）是從稽核日誌重建的。問戰情室的對話記憶（convo.py）只記 id，不存訂單資料，每次重查。
證據: `warroom/agent/main_agent.py:65`, `warroom/agent/main_agent.py:67`, `warroom/agent/main_agent.py:68`, `warroom/agent/convo.py:1`, `warroom/api/daylog.py:1`
關鍵詞: 記憶, memory, 對話歷史, 前情摘要, summary, pins, 釘選, 長期記憶, task log

## Q: 主管在後台寫的機台備註，agent 會讀嗎？
A: 目前不會。memory_notes 是後台寫的每台機台備註，但 warroom/agent 裡沒有任何地方讀它，也沒有接進 prompt；未來要做成讀取工具。
證據: `warroom/data/memory_notes.py:3`
關鍵詞: memory notes, 機台備註, 後台, 長期記憶, 知識累積, 未接入

## Q: 什麼是預先規劃（pre-planning）？
A: 有風險的訂單（可能晚交）在主管打開之前就先擬好計畫，主管只要按核准或討論。runtime 每 30 秒（模擬時間）看一次，同時最多 2 件，在 ModelQueue 裡排在事故之後。只改變「何時開始規劃」，跑的仍是正常的 PLAN → GATE → 核准流程。
證據: `warroom/api/runtime.py:1091`, `warroom/api/runtime.py:1092`, `warroom/api/runtime.py:1116`, `tests/test_preplan.py:1`
關鍵詞: preplan, 預先規劃, 主動, proactive, 待核准, 訂單晚交, 核准區

## Q: 只有一顆模型，多件事同時發生怎麼排？
A: ModelQueue 管一個本地模型的呼叫槽位（預設 1 個），依優先序再依到達順序。優先序：safety 0、機台 1、訂單／原料／主管任務 2、人力和早班 3、提問和預先規劃 4、閒聊 5。等待時間會寫稽核。
證據: `warroom/agent/orchestrator.py:126`, `warroom/agent/orchestrator.py:129`, `warroom/agent/orchestrator.py:52`
關鍵詞: ModelQueue, 優先序, priority, 併發, concurrency, 排隊, 單模型, 資源排程

## Q: 計畫怎麼驗證是對的？
A: 三關：SIMULATE 用硫化公式和 OR-Tools 排程求解，Gate 要求寫入必須有可行的模擬，confirm_schedule 只能確認這個事件可行求解產生的版本；執行後 VERIFY 是程式輪詢讀值，跟程式建立或限定的預期值比對，不符就否定假設進 REPLAN（例如 119.5°C → H1 不成立）。另外 plan_check.py 每分鐘比對計畫和實際（出模晚、入模晚、偏離排程），落後就喚醒指揮官。
證據: `warroom/agent/gate.py:24`, `warroom/agent/gate.py:310`, `warroom/agent/state_machine.py:12`, `warroom/detect/plan_check.py:1`
關鍵詞: 驗證, validation, SIMULATE, VERIFY, 可行性, feasibility, 求解器, OR-Tools, plan vs actual, 假設檢驗, 計劃有效, 確認計劃, AI 計劃對不對, 計劃驗證, 怎麼知道計劃可行

## Q: 斷網還能跑嗎？
A: 可以。模型走本地 OpenAI 相容端點（Spark 上 vLLM、Mac 上 Ollama），SOP 知識庫用 Qdrant（Mac 內嵌、Spark 本機服務）加 fastembed 嵌入，模型快取後完全離線；嵌入模型載不到時降級成純字詞檢索。拔網路線的正式判定由 bench/unplug_test 跑，tests/test_unplug_network.py 檢查防火牆規則。實體拔線在 Spark 上的判定還沒跑過。
證據: `warroom/agent/commander.py:3`, `warroom/kb/store.py:1`, `warroom/kb/store.py:9`, `README.md:118`, `tests/test_unplug_network.py:1`, `README.md:202`
關鍵詞: 斷網, offline, 離線, air-gapped, 地端, fastembed, Qdrant, unplug, 拔網路線, 網路斷掉, 沒網路, 斷線, 還能動嗎, 不連網

## Q: AI 會不會亂改產線？會不會自己下指令給機台？
A: 不會。AI 只產出計畫，寫入只在 EXECUTE 狀態、只有三個寫入工具（confirm_schedule、set_cure_time、notify_teams），而且都要過確定性的 Action Gate；R2、R3 的變更一定要主管核准。系統裡沒有任何致動器或對 PLC 下寫入的工具，安全核心是獨立的程式規則，LLM 呼叫不到也解除不了它的停機。
證據: `warroom/contracts.py:197`, `warroom/contracts.py:206`, `warroom/agent/gate.py:1`, `warroom/agent/gate.py:13`, `warroom/contracts.py:225`, `warroom/safety/core.py:1`, `warroom/agent/state_machine.py:37`
關鍵詞: 亂改產線, 亂改, 失控, 自己下指令, 控制機台, 自主, autonomous, 改產線, 安全性, 誤動作

## Q: 指揮官（AI）可以做哪些事？做不到什麼？
A: 指揮官從不自己執行，它只寫計劃；狀態機在 Gate 把關、主管核准後才去執行。整個系統只有三個寫入工具：confirm_schedule 確認排程、set_cure_time 設硫化時間建議值、notify_teams 通知各組，而且 set_cure_time 只是記在工單上的建議值，不是寫 PLC。它做不到：跟供應商叫料、改客戶交期、直接控制機台或 PLC、解除安全核心停機（只有主管能按確認復機）、找人或加人；遇到這些，計劃只能通知相關的人，或建議主管去做。這是刻意的：寫入少，每一個都能被管住；要加新能力（例如自動叫料），就做成一個新的工具模組，配上自己的風險等級和 Gate 規則，我們的說法是「工具模組化：限縮才能完全掌控」（團隊說法，出自簡報）。目前的缺口：原料缺料只會更新庫存，還不會自動開事件（#38 已關閉）。
證據: `warroom/agent/state_machine.py:9`, `warroom/agent/state_machine.py:10`, `warroom/contracts.py:197`, `warroom/tools/impl.py:314`, `warroom/tools/specs.py:73`, `warroom/api/app.py:649`, `warroom/api/runtime.py:1694`
關鍵詞: 指揮官, commander, 能做什麼, 做不到, 叫料, 採購, 改交期, 交期, 寫入, 三種寫入, 只有三個寫入, 權限, 限縮, 掌控, 工具模組, 新能力, 自動叫料, 缺料

## Q: 主管到底核准什麼？
A: 主管核准 R2 和 R3 的寫入：R2 是排程、硫化時間建議、加班、派工；R3 是放慢或暫停投料、跨組調人、機台停用。主管可以核准、修改或駁回，決定會寫稽核並回到等待中的事件；也可以用文字討論計畫，每個事件最多因討論重排 MAX_DISCUSS_REPLANS = 3 次。R0 讀取和 R1 模板通知自動執行。
證據: `warroom/contracts.py:225`, `warroom/contracts.py:226`, `warroom/agent/approvals.py:3`, `warroom/contracts.py:172`, `README.md:33`
關鍵詞: 主管核准, human in the loop, approval, 人在迴圈, 駁回, 修改, 討論計畫, 手機核准, 一定要核准, 哪些動作要核准, 按核准, 需要人同意

## Q: 換一間工廠要怎麼導入？
A: 工廠習慣和用語寫在 config/plant/PLANT.md；SOP 放進 sop_dir（預設 warroom/kb/sop，.md 或 .txt）會重建索引；團隊資料集的設備 id 透過 warroom/data/team_ids.py 對應到系統機台 id。（未驗證：沒有找到完整的新廠導入文件；機台清單本身寫在 warroom/factory.py，換廠需要改程式。）
證據: `config/plant/PLANT.md:1`, `warroom/config.py:45`, `warroom/kb/store.py:114`, `warroom/data/team_ids.py:1`
關鍵詞: 導入, onboarding, 新工廠, 擴展, scale, PLANT.md, SOP, team_ids, 客製

## Q: 你們的 QA 模式／RAG 怎麼避免亂答？
A: 這個 QA 模式會依檢索分數加關鍵詞重疊做門檻，沒達標就說查不到、不硬答。SOP 知識庫的「無答案門檻」正在另一個分支加入，還沒進這個分支。現有的 SOP 檢索是混合式：嵌入相似度加字元 bigram 字詞分數，每份 SOP 回一個最佳段落並附 sop_id、標題、檔名當出處。
證據: `warroom/kb/store.py:4`, `warroom/kb/store.py:7`
關鍵詞: RAG, 檢索, no-answer, 無答案, 門檻, threshold, 引用, citation, 幻覺, QA 模式
