// 自動播放 Demo（CURE-07 單一事件）的開關與時間表。Hotfix 2026-10-22.
// 只在「瀏覽器內建 Mock 引擎」跑：網址要同時有 ?mock=1 和 ?demo=cure07。核准只存在本機記憶體，不碰 Action Gate、
// 不送主管身分、不寫後端稽核。Live（真實後端）永遠不會自動核准，維持原本的人工流程。
// 例：/factory?mock=1&scenario=press7_temp_drop&demo=cure07
export const AUTO_Q = 'demo'
export const AUTO_VAL = 'cure07'

const q = () => (typeof location === 'undefined' ? null : new URLSearchParams(location.search))
/** 網址要求自動播放（且是 Mock）。每個頁面（3D、戰情局 iframe、手機 iframe）都帶同樣的參數。 */
export const autoRequested = (): boolean => {
  const p = q()
  return !!p && p.has('mock') && p.get(AUTO_Q) === AUTO_VAL
}
/** 真的可以自動播放：網址要求，而且 transport 確實是 mock。 */
export const autoActive = (mode: string | undefined): boolean => autoRequested() && mode === 'mock'

if (autoRequested() && typeof document !== 'undefined') document.documentElement.dataset.demoAuto = '1'

/** 時間表（秒，Demo 時鐘）。事件在 Mock 情境裡的時間見 mock/scripts.ts（single 版）。 */
export const AUTO = {
  /** 3D 工廠開場 */
  openS: 4.5,
  /** 事件出現後，Agent 分析與建議給觀眾看多久，才自動核准 */
  analysisS: 18,
  /** 情境速度：工廠秒 / Demo 秒 */
  speed: 10,
  /** 手機在事件出現後多久浮出（秒） */
  phoneAfterS: 1.5,
}

/** 自動播放走的故事步驟（cues.ts 的 id，依 CUES 順序）；其餘（急單、預排、H1/H2、機器人、Pitch）略過。 */
export const AUTO_CUES = ['start', 'event', 'agent']
/** 最後一步：停在最終結果，不往 Pitch 走。 */
export const AUTO_FINAL = 'agent'

export const AUTO_SCENARIO = 'press7_temp_drop'
export const CURE07_FROM = 128
