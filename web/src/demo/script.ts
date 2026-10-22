// 提詞機台詞：一個 beat 一句。CJ 改講稿只要改這裡的文字（beat id 不要動，是 Demo 流程的固定介面）。
// beat 的型別與順序只定義一次，在 ./beat.ts（auto 流程的固定介面）；這裡轉出去給提詞機用。
import { DEMO_BEATS, type DemoBeat } from './beat'
export type { DemoBeat }

/** Demo 的順序（進度條 = 第幾段 / BEATS.length）＝ beat.ts 的 DEMO_BEATS。 */
export const BEATS: readonly DemoBeat[] = DEMO_BEATS

export const SCRIPT: Record<DemoBeat, string> = {
  overview: '大家好，我們是新創團隊 Team Westie。我們要讓中小企業工廠真的用得上邊緣 AI：一台 DGX Spark，資料不出廠。這是一條實心胎，光是硫化就要 24 到 34 個小時，工廠的人每天忙得焦頭爛額。',
  walk: '我們來看看，我們的產品為他們帶來什麼改變。',
  orders: '早上一進來，桌上滿滿的訂單。以前要手算、一筆一筆 key 進電腦；現在 Agent Nemo 找出排訂單的最佳解，排程是求解器算的，不是 AI 猜的。方案擬好，主管看一眼就核准。',
  leave: '訂單處理好了，我們到現場走走。',
  anomaly: '啊，臨時事件發生了：7 號硫化機溫度往下掉。事件狀態機先分類、定出風險等級，再到 SOP 資料庫查處置方式，排出解法。',
  phone: '手機響了：會動到排程和人力的動作，才需要主管核准，一鍵核准。',
  repair: '核准之後，程式把方案變成人員任務：誰、去哪、做什麼、幾點完成。員工跑去維修，機台恢復正常。',
  robot: '巡檢機器人也在現場巡邏，跟大家打個招呼。',
  back: '回到戰情室，接下來我直接操作給大家看。',
}

export interface PrompterLine {
  /** 0-based position in BEATS. */
  index: number
  total: number
  current: string
  /** The next beat's line, or null on the last beat. */
  next: string | null
  /** (index + 1) / total — the bar is full on the last beat. */
  progress: number
}

/** Current + next line for a beat; null when there is no beat (prompter renders nothing). */
export function lineFor(beat: DemoBeat | null | undefined): PrompterLine | null {
  if (!beat) return null
  const index = BEATS.indexOf(beat)
  if (index < 0) return null
  const nb = BEATS[index + 1]
  return { index, total: BEATS.length, current: SCRIPT[beat], next: nb ? SCRIPT[nb] : null, progress: (index + 1) / BEATS.length }
}
