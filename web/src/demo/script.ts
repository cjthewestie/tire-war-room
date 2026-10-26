// 提詞機台詞：一個 beat 一句。CJ 改講稿只要改這裡的文字（beat id 不要動，是 Demo 流程的固定介面）。
// beat 的型別與順序只定義一次，在 ./beat.ts（auto 流程的固定介面）；這裡轉出去給提詞機用。
import { DEMO_BEATS, type DemoBeat } from './beat'
export type { DemoBeat }

/** Demo 的順序（進度條 = 第幾段 / BEATS.length）＝ beat.ts 的 DEMO_BEATS。 */
export const BEATS: readonly DemoBeat[] = DEMO_BEATS

export const SCRIPT: Record<DemoBeat, string> = {
  // HY 的定稿（CJ 10-22：「逐字稿要放進3d網頁提詞機」）
  overview: '大家好，我們是 Team Westie。我們希望讓中小工廠真正用得上邊緣 AI，把 AI 帶進工廠。今天沒辦法真進工廠，所以我們打造了一座「虛擬工廠」，帶大家一起進入這個戰場！',
  walk: '請看這條實心胎。硫化是整個製程最關鍵、最耗時，而且不可逆的工序，一次就要二十四到三十四小時。一台機器出狀況，就可能牽動整批訂單的交期。這就是實心胎製程的主戰場。',
  orders: '假想你是工廠的主管。早上一進辦公室，桌上就是今天滿滿的訂單。以前要自己算排程，再一筆一筆 key 單。現在不一樣，我們做了 Agent Nemo，這位 AI 參謀，會先分析訂單，再呼叫求解器算出最佳排程。排程是求解器計算，不是 AI 猜想。你身為主管，只要看一眼，確認沒問題，按下核准就好。',
  leave: '排程排好了，主管離開電腦，到現場走走。這時候整座工廠一切正常。',
  phone: '啊，臨時事件發生了：7 號硫化機溫度往下掉。不管你人在哪裡，茶水間還是廁所，手機都收得到。事件狀態機先分類、定出風險等級，再到 SOP 資料庫查處置方式，排出解法。',
  approve: '會動到排程和人力的 R2、R3 動作，才需要主管核准：一鍵核准。手機上的卡號，跟主控台的任務編號是同一個。不喜歡這個方案？直接跟 Nemo 說，例如「不要跨組調人」，它會重排、發一張新卡。',
  repair: '核准之後，程式把方案變成人員任務：誰、去哪、做什麼、幾點完成。員工到 7 號機維修，維修完成，訂單接著跑。',
  robot: '這是我們的下一步：巡檢機器人。夜班和高風險的區域，讓它先去看，有狀況一樣回報給 Nemo。',
  back: '回到辦公室，畫面一切正常，終於可以安心吃早餐了！順便看看 Agent 的任務紀錄：它做了什麼、為什麼這樣決定、誰核准的，都有紀錄。後台也可以修改工廠的指令，安全規則和風險等級寫在程式裡，後台改不到。好，這場早晨的生產危機，總算順利解除！',
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
