// The auto-play demo's current beat (/factory?demo=cure07, the real backend; ?mock=1 as the fallback): one small, stable
// API for anything that narrates the run (the presenter's prompter). CJ's flow, in order:
//   overview 俯瞰 → walk 走向電腦 → orders 日常訂單在電腦上排好、完成 → leave 離開電腦、往工廠那邊看（7 號機警示燈亮起）→
//   phone 手機響（就是 7 號機異常通知）、核准 → repair 看員工做完任務、7 號機轉綠 → robot 跟巡檢機器人互動 →
//   back 走回戰情室電腦（自動播放到此結束）
// (CJ 10-22: 「我離開電腦之後往工廠那邊看，手機響，然後核准，看員工做完任務，跟巡檢機器人互動，然後回到電腦前」.)
// The 3D's auto driver (factory3d/useGuidedDemo.ts) sets it every tick from the timeline (demo/cues.ts advance(…, auto)):
// beatOf() maps the cue (auto.ts AUTO_CUES), whether the lead has reached the desk and whether the cue's hold is over
// to a beat. null outside the auto demo (the manual ▶ Demo and normal use never set one). The ids key the prompter's
// lines (script.ts) and the captions (auto.ts AUTO_TEXT): change all three together.
import { useSyncExternalStore } from 'react'

export type DemoBeat = 'overview' | 'walk' | 'orders' | 'leave' | 'phone' | 'approve' | 'repair' | 'robot' | 'back'
export const DEMO_BEATS: readonly DemoBeat[] = ['overview', 'walk', 'orders', 'leave', 'phone', 'approve', 'repair', 'robot', 'back']

/** Which auto cue each beat starts on (cues.ts ids). walk, orders and leave start on the 訂單 cue: walk until the lead
 *  is at the desk, orders while the day's orders are planned there, leave once that hold is over (the lead turns out to
 *  the factory); leave goes on through the 7 號機 cue (the alarm light comes on), the phone rings at h1. */
export const BEAT_CUE: Record<DemoBeat, string> = {
  overview: 'start', walk: 'start', orders: 'rush', leave: 'rush', phone: 'h1', approve: 'h1', repair: 'worker', robot: 'robot', back: 'agent',
}

/** The beat for an auto cue; `atDesk` = the lead has walked to the war-room computer (splits walk / orders);
 *  `released` = the cue's hold is over (the 急單 cue: the order is done, the lead leaves the desk). */
export function beatOf(cueId: string | null | undefined, atDesk = true, released = false,
  x: { elapsed?: number; overviewS?: number; approved?: boolean } = {}): DemoBeat | null {
  switch (cueId) {
    // the opening shot carries two lines (CJ's 逐字稿): 開場, then 鏡頭飛過產線 (from overviewS s in)
    case 'start': return x.overviewS != null && (x.elapsed ?? 0) >= x.overviewS ? 'walk' : 'overview'
    case 'rush': return released ? 'leave' : atDesk ? 'orders' : 'walk'
    case 'event': return 'leave'
    // the phone rings (the 7 號機 line), then, once its card is approved, 一鍵核准
    case 'h1': return x.approved ? 'approve' : 'phone'
    case 'worker': return 'repair'
    case 'robot': return 'robot'
    case 'agent': return 'back'
    default: return null
  }
}

/** The six stages CJ names (「訂單 / 離開電腦 / 7號機手機 / 維修 / 機器人 / 回到電腦」): the indicator 「2/6 離開電腦」. */
export const STAGES = ['訂單', '離開電腦', '7號機手機', '維修', '機器人', '回到電腦'] as const
export function stageOf(b: DemoBeat | null): { n: number; total: number; label: string } | null {
  if (!b) return null
  const i = b === 'overview' || b === 'walk' || b === 'orders' ? 0 : b === 'leave' ? 1 : b === 'phone' || b === 'approve' ? 2
    : b === 'repair' ? 3 : b === 'robot' ? 4 : 5
  return { n: i + 1, total: STAGES.length, label: STAGES[i] }
}

let current: DemoBeat | null = null
const subs = new Set<() => void>()

/** The driver's side (useGuidedDemo): set the beat, or null when the auto demo stops. */
export function setDemoBeat(b: DemoBeat | null): void {
  if (b === current) return
  current = b
  if (typeof document !== 'undefined') {
    if (b) document.documentElement.dataset.demoBeat = b
    else delete document.documentElement.dataset.demoBeat
  }
  for (const f of subs) f()
}
export const getDemoBeat = (): DemoBeat | null => current
export function subscribeDemoBeat(f: () => void): () => void { subs.add(f); return () => { subs.delete(f) } }

/** The current auto-demo beat, or null outside the auto demo. Re-renders only when the beat changes. */
export function useDemoBeat(): DemoBeat | null {
  return useSyncExternalStore(subscribeDemoBeat, getDemoBeat, () => null)
}
