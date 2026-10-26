// 自動播放 Demo（CURE-07）的開關與時間表。CJ 2026-10-22：「demo 完的資料要留在主機裡，這樣才能查看他剛剛的任務」。
// 預設跑真實後端：/factory?demo=cure07 —— 情境經 /api/sim/control 載入（伺服器現在是 rules 或 LLM 就用那個），
// 事件、核准、員工任務、稽核全部存在主機上，跑完之後主控台／Agent 頁／任務清單（/api/tasks）都查得到。
// 真實後端「不會」自動核准：核准一定是人在手機（或戰情室核准卡）上按，走原本的 Action Gate 與主管身分；Demo 只把手機
// 舉起來、等那一下。備援：/factory?mock=1&scenario=press7_temp_drop&demo=cure07 —— 瀏覽器內建 Mock 引擎，核准只存在
// 本機記憶體（Demo 自動核准，不碰 Action Gate、不送主管身分、不寫後端稽核）。
// 八段流程（beat.ts）：俯瞰 → 走向電腦 → 日常訂單在電腦上排好 → 離開電腦往工廠看 → 手機響（7 號機異常）、核准 →
// 看員工做完任務 → 巡檢機器人 → 走回戰情室電腦（結束）。CJ 10-22：「我 3d demo 只需要 7 號溫度這個核准異常」：
// 真實後端載 7 號機溫降情境（沒有急單卡），隨機事件關掉；整段只有 7 號機的核准、手機只響這一次。
import type { DemoBeat } from './beat'
import type { Signal } from './cues'
import { SCRIPT } from './script'

export const AUTO_Q = 'demo'
export const AUTO_VAL = 'cure07'

const q = () => (typeof location === 'undefined' ? null : new URLSearchParams(location.search))
/** 網址要求自動播放（?demo=cure07；真實後端或 ?mock=1 都算）。每個頁面（3D、戰情局 iframe、手機 iframe）都帶同樣的參數。 */
export const autoRequested = (): boolean => q()?.get(AUTO_Q) === AUTO_VAL
/** 網址指定 Mock 備援（?mock=1&demo=cure07）：Mock 引擎用單一事件版情境、Demo 自動核准。 */
export const autoMockRequested = (): boolean => { const p = q(); return !!p && p.has('mock') && p.get(AUTO_Q) === AUTO_VAL }
/** 可以自動播放：網址要求，且有 transport（真實後端或 Mock）。 */
export const autoActive = (mode: string | undefined): boolean => autoRequested() && (mode === 'mock' || mode === 'live')
/** 按鈕全藏的 Mock 自動卡（phone/AutoCard）只在 Mock 引擎。真實後端的自動播放也會自動核准（CJ 10-22：「3d 工廠 demo 核准要
 *  自動核准」），但只核准 Demo 自己的 7 號機卡、走主管手機同一支核准 API（Action Gate、稽核照舊），手機上看得到完整的卡。 */
export const autoApproves = (mode: string | undefined): boolean => autoRequested() && mode === 'mock'
/** 真實後端的自動播放：Demo 替主管在手機上按 7 號機的核准（driver.ts autoStep）。 */
export const autoApprovesLive = (mode: string | undefined): boolean => autoRequested() && mode === 'live'

// html[data-demo-auto]：自動播放的畫面精簡（auto.css）。"mock" 才藏核准按鈕（Demo 自動核准）；真實後端要留著給人按。
if (autoRequested() && typeof document !== 'undefined') document.documentElement.dataset.demoAuto = autoMockRequested() ? 'mock' : 'live'

/** 時間表（秒，Demo 時鐘）。Mock 情境裡事件的時間見 mock/scripts.ts（single 版）與 mock/engine.ts。 */
export const AUTO = {
  /** 3D 工廠開場（俯瞰） */
  openS: 4.5,
  /** 事件出現後，Agent 分析與建議給觀眾看多久，才自動核准（Mock） */
  analysisS: 18,
  /** Mock 情境速度：工廠秒 / Demo 秒 */
  speed: 10,
  /** 手機在事件出現後多久浮出（秒） */
  phoneAfterS: 1.5,
  /** 急單的計劃卡在螢幕上給觀眾看多久，才自動核准（Mock） */
  ordersApproveS: 4,
  /** 真實後端：訂單任務的核准卡在主控台上給觀眾看幾秒，Demo 才在主控台替主管按核准（via dashboard） */
  consoleApproveS: 3,
  /** 真實後端：7 號機的卡在手機上給觀眾看幾秒，Demo 才替主管按核准（同一支核准 API，過 Action Gate、留稽核） */
  liveApproveS: 3,
  /** 訂單段至少停多久（走到電腦後） */
  ordersMinS: 3,
  /** 訂單解決後，離開電腦往 7 號機走至少多久，異常這段才開始（異常早就發生也一樣：先走出去） */
  leaveS: 4,
  /** 走到 7 號機附近、看著異常至少多久，手機才響 */
  anomalyS: 4,
  /** 維修段至少多久（CJ：「七號機維修等太久」：走幾秒、很快修好就往機器人走） */
  repairMinS: 7,
}

/** 每段至少停多久（秒）：照提詞機那句的長度（約每秒 4 個字，CJ 唸得完），最少 7 秒。CJ 10-22：「每一個步驟不可以太趕，要順暢」。 */
export const SPEECH_CPS = 4
export const BEAT_MIN_FLOOR = 7
export const beatMinS = (b: DemoBeat): number => Math.max(BEAT_MIN_FLOOR, Math.ceil(SCRIPT[b].replace(/\s/g, '').length / SPEECH_CPS))
/** 動作完成之後，結果在畫面上至少再留幾秒（訂單結案、手機上的「已核准」、7 號機轉綠） */
export const RESULT_S = { orders: 5, phone: 3, repair: 3 }

/** 自動播放走的故事步驟（cues.ts 的 id，依 CUES 順序）；console／verify／h2／Pitch 略過。 */
export const AUTO_CUES = ['start', 'rush', 'event', 'h1', 'worker', 'robot', 'agent']
/** 最後一步：回到戰情室電腦，停住，留給 CJ 手動 Demo（不往 Pitch 走、不再自動做任何事）。 */
export const AUTO_FINAL = 'agent'

/** 每一步怎麼進、怎麼停（cues.ts advanceAuto 讀這張表）。
 *  enter.on：等這個訊號才進（或 timeout 秒後照進）；hold：至少 min 秒，且（needArrive）走到位、`until` 訊號到了才放。
 *  card：等的核准卡；卡一直沒出現 cardTimeout 秒就不等了（情境沒有這張卡）。卡出現了就等人（真實後端）或 Mock 自動核准，
 *  最多 timeout 秒。 */
export interface AutoStep {
  /** min：上一步結束後至少過幾秒才進（離開電腦的那段路） */
  enter?: { on: Signal; timeout: number; min?: number }
  /** untilDwell：`until` 到了之後，結果至少再留這麼多秒才放（看得到結果） */
  hold: { min: number; needArrive?: boolean; until?: Signal; untilDwell?: number; card?: Signal; cardTimeout?: number; timeout?: number }
}
export const AUTO_STEPS: Record<string, AutoStep> = {
  // 俯瞰：開場那句唸完（真實後端：沒有急單卡，訂單段只看日常訂單；Mock 的急單卡照舊自動核准）
  start: { hold: { min: Math.max(AUTO.openS, beatMinS('overview')) } },
  // 走向電腦（walk）→ 電腦上急單進來、計劃、核准、完成（orders）；結案後畫面再留 RESULT_S.orders 秒
  // 真實後端（CJ 10-22：「是在主控台跑訂單任務跟核准」）：走向電腦時 Demo 開始今日訂單排程（早班開工計畫），主控台上
  // Agent Nemo 規劃 → 核准卡 → Demo 在主控台替主管按核准 → 執行 → 結案；結案後再留 RESULT_S.orders 秒。到了電腦最多再等
  // ORDERS_CAP_S 秒，模型慢也照樣往下（任務在背景跑完）。
  // (until rushRunning, not rushDone: the day's plan stays 驗證中 while the crews work through their tasks — the beat
  // used to wait for that until its 64 s cap, which looked stuck; the crews finish in the background, sim at 120×)
  rush: { hold: { min: beatMinS('walk') + beatMinS('orders'), needArrive: true, until: 'rushRunning', untilDwell: RESULT_S.orders, card: 'rushCard', cardTimeout: 45, timeout: beatMinS('walk') + beatMinS('orders') + 25 } },
  // 訂單結束 → 離開電腦、走到門口往工廠看（leave）→ 7 號機警示燈亮（事件進來，還是 leave）→ 看一下，手機才響
  event: { enter: { on: 'event', timeout: 90, min: AUTO.leaveS }, hold: { min: AUTO.anomalyS, needArrive: true, timeout: 30 } },
  // 手機這時才響（震動＋鈴聲＋舉起來）→ 核准：Mock 由 Demo 自動按；真實後端等人按
  // 核准後手機留著「已核准」RESULT_S.phone 秒才放下
  h1: { enter: { on: 'approval', timeout: 60 }, hold: { min: beatMinS('phone'), until: 'approved', untilDwell: RESULT_S.phone, card: 'approval', cardTimeout: 30, timeout: 600 } },
  // 員工跑去 7 號機：前往中 → 執行中 → 完成，機台轉綠
  // 核准後員工馬上出發；任務完成（不等溫度完全回升，回升在背景繼續）就轉綠、再留 RESULT_S.repair 秒（CJ：「七號機維修等太久」）
  worker: { enter: { on: 'approved', timeout: 5 }, hold: { min: AUTO.repairMinS, needArrive: true, until: 'crewDone', untilDwell: RESULT_S.repair, timeout: 25 } },
  // 巡檢機器人：轉身、揮手、掃描、鞠躬（walk 的 arrived 含機器人表演完）
  // 巡檢機器人：整套表演做完（arrived）、那句唸完，才走回電腦
  robot: { hold: { min: beatMinS('robot'), needArrive: true, timeout: 30 } },
  agent: { hold: { min: 0 } },
}

/** Mock 備援情境（單一事件版：急單 + 7 號機一輪核准）。 */
export const AUTO_SCENARIO = 'press7_temp_drop'
/** 真實後端情境：只有 7 號機溫降（CJ：「只需要 7 號溫度這個核准異常」）。 */
/** CJ 10-22：「為什麼一開始硫化機就出問題」「一開始是正常的一天」：真實後端從正常運轉開始（沒有排好的故障、隨機事件關），
 *  到了 leave（離開電腦往工廠看）才由 Demo 注入 7 號機蒸汽壓下降（POST /api/sim/fault，以前觸發異常的同一條路）。 */
export const AUTO_LIVE_SCENARIO = 'normal'
export const AUTO_LIVE_SPEED = 15
/** 真實後端：走向電腦時開始的今日訂單排程（早班開工計畫，POST /api/sim/events/fire shift_start；CJ：「呈現一種日常訂單，
 *  早上 agent 安排完」「急單不要了」） */
export const AUTO_ORDER_EVENT = 'shift_start'

/** Demo 在 leave 注入的故障：7 號機蒸汽壓不足，128 → 122°C（不洩壓：只有一張核准卡） */
export const AUTO_FAULT = { machine_id: 'CURE-07', kind: 'steam_pressure_drop', params: { target_c: 122 } }
/** 回到電腦、Demo 結束：模擬速度回 1×、清掉 7 號機的故障（全廠正常），留給 CJ 手動 Demo 各頁面 */
export const AUTO_END_SPEED = 1
/** 模擬在 Demo 等工廠的時候跑快一點（不要空等），其他時候 AUTO_LIVE_SPEED：今日訂單排程核准後到結案（人員任務做完、
 *  轉綠）、7 號機注入後到偵測、7 號機核准後到員工做完。 */
export const AUTO_BOOST = { orderDone: 120, detect: 60, repair: 90 }
export const CURE07_FROM = 128

/** 自動播放上方的一行字幕（每段一句；AutoOverlay 依 useDemoBeat() 顯示）。 */
export const AUTO_TEXT: Record<DemoBeat, string> = {
  overview: '工廠運轉中',
  walk: '走向戰情室電腦',
  orders: '📋 今日訂單排程：Agent 規劃 → 主管核准 → 完成',
  leave: '🚶 訂單處理好了：離開電腦，往工廠看',
  phone: '📱 手機響了：7 號硫化機溫度異常，主管核准',
  repair: '👷 員工前往 7 號機維修',
  robot: '🤖 巡檢機器人',
  back: '回到戰情室：剛剛的任務都在這裡',
}
