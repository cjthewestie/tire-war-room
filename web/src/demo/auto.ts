// 自動播放 Demo（CURE-07）的開關與時間表。CJ 2026-10-22：「demo 完的資料要留在主機裡，這樣才能查看他剛剛的任務」。
// 預設跑真實後端：/factory?demo=cure07 —— 情境經 /api/sim/control 載入（伺服器現在是 rules 或 LLM 就用那個），
// 事件、核准、員工任務、稽核全部存在主機上，跑完之後主控台／Agent 頁／任務清單（/api/tasks）都查得到。
// 真實後端「不會」自動核准：核准一定是人在手機（或戰情室核准卡）上按，走原本的 Action Gate 與主管身分；Demo 只把手機
// 舉起來、等那一下。備援：/factory?mock=1&scenario=press7_temp_drop&demo=cure07 —— 瀏覽器內建 Mock 引擎，核准只存在
// 本機記憶體（Demo 自動核准，不碰 Action Gate、不送主管身分、不寫後端稽核）。
// 八段流程（beat.ts）：俯瞰 → 走向電腦 → 訂單任務進來並解決 → 轉頭異常 → 按手機核准 → 員工跑去修 → 巡檢機器人 → 回到戰情室（結束）。
import type { DemoBeat } from './beat'
import type { Signal } from './cues'

export const AUTO_Q = 'demo'
export const AUTO_VAL = 'cure07'

const q = () => (typeof location === 'undefined' ? null : new URLSearchParams(location.search))
/** 網址要求自動播放（?demo=cure07；真實後端或 ?mock=1 都算）。每個頁面（3D、戰情局 iframe、手機 iframe）都帶同樣的參數。 */
export const autoRequested = (): boolean => q()?.get(AUTO_Q) === AUTO_VAL
/** 網址指定 Mock 備援（?mock=1&demo=cure07）：Mock 引擎用單一事件版情境、Demo 自動核准。 */
export const autoMockRequested = (): boolean => { const p = q(); return !!p && p.has('mock') && p.get(AUTO_Q) === AUTO_VAL }
/** 可以自動播放：網址要求，且有 transport（真實後端或 Mock）。 */
export const autoActive = (mode: string | undefined): boolean => autoRequested() && (mode === 'mock' || mode === 'live')
/** 自動核准只在 Mock 引擎（本機記憶體）；真實後端永遠是人按。 */
export const autoApproves = (mode: string | undefined): boolean => autoRequested() && mode === 'mock'

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
  /** 訂單段至少停多久（走到電腦後） */
  ordersMinS: 3,
  /** 員工修好後，在 7 號機前多看一下（綠燈） */
  repairMinS: 3,
}

/** 自動播放走的故事步驟（cues.ts 的 id，依 CUES 順序）；console／verify／h2／Pitch 略過。 */
export const AUTO_CUES = ['start', 'rush', 'event', 'h1', 'worker', 'robot', 'agent']
/** 最後一步：回到戰情室電腦，停住，留給 CJ 手動 Demo（不往 Pitch 走、不再自動做任何事）。 */
export const AUTO_FINAL = 'agent'

/** 每一步怎麼進、怎麼停（cues.ts advanceAuto 讀這張表）。
 *  enter.on：等這個訊號才進（或 timeout 秒後照進）；hold：至少 min 秒，且（needArrive）走到位、`until` 訊號到了才放。
 *  card：等的核准卡；卡一直沒出現 cardTimeout 秒就不等了（情境沒有這張卡）。卡出現了就等人（真實後端）或 Mock 自動核准，
 *  最多 timeout 秒。 */
export interface AutoStep {
  enter?: { on: Signal; timeout: number }
  hold: { min: number; needArrive?: boolean; until?: Signal; card?: Signal; cardTimeout?: number; timeout?: number }
}
export const AUTO_STEPS: Record<string, AutoStep> = {
  start: { hold: { min: AUTO.openS } },
  // 走向電腦（walk）→ 電腦上急單進來、計劃、核准、完成（orders）
  rush: { hold: { min: AUTO.ordersMinS, needArrive: true, until: 'rushDone', card: 'rushCard', cardTimeout: 40, timeout: 240 } },
  // 轉頭：7 號機異常（手機在 phoneAfterS 後浮出）
  event: { enter: { on: 'event', timeout: 90 }, hold: { min: AUTO.phoneAfterS + 0.5 } },
  // 按手機核准：Mock 由 Demo 自動按；真實後端等人按
  h1: { enter: { on: 'approval', timeout: 60 }, hold: { min: 1, until: 'approved', card: 'approval', cardTimeout: 30, timeout: 600 } },
  // 員工跑去 7 號機：前往中 → 執行中 → 完成，機台轉綠
  worker: { enter: { on: 'approved', timeout: 5 }, hold: { min: AUTO.repairMinS, needArrive: true, until: 'repaired', timeout: 75 } },
  // 巡檢機器人：轉身、揮手、掃描、鞠躬（walk 的 arrived 含機器人表演完）
  robot: { hold: { min: 3.2, needArrive: true, timeout: 15 } },
  agent: { hold: { min: 0 } },
}

/** Mock 備援情境（單一事件版：急單 + 7 號機一輪核准）。 */
export const AUTO_SCENARIO = 'press7_temp_drop'
/** 真實後端情境：訂單湧入、急單、7 號機溫降（PR #107）。伺服器沒有這個情境時改用 press7_temp_drop。 */
export const AUTO_LIVE_SCENARIO = 'morning_rush'
export const AUTO_LIVE_SPEED = 40
export const CURE07_FROM = 128

/** 自動播放上方的一行字幕（每段一句；AutoOverlay 依 useDemoBeat() 顯示）。 */
export const AUTO_TEXT: Record<DemoBeat, string> = {
  overview: '工廠運轉中',
  walk: '走向戰情室電腦',
  orders: '📋 訂單任務進來：Agent 規劃 → 核准 → 完成',
  anomaly: '⚠ 7 號硫化機溫度異常（CURE-07）',
  approve: '📱 主管在手機上核准',
  repair: '👷 員工前往 7 號機維修',
  robot: '🤖 巡檢機器人',
  warroom: '回到戰情室：剛剛的任務都在這裡',
}
