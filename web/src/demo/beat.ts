// The auto-play demo's current beat (/factory?demo=cure07, the real backend; ?mock=1 as the fallback): one small, stable
// API for anything that narrates the run (the presenter's prompter). CJ's flow, in order:
//   overview 俯瞰 → walk 走向電腦 → orders 訂單任務進來然後解決 → leave 離開電腦、走向工廠 → anomaly 走路途中 7 號機異常 →
//   phone 手機才響、拿起來核准 → repair 去看員工修好 7 號機 → robot 跟巡檢機器人互動 → back 走回戰情室電腦（自動播放到此結束）
// (CJ 10-22: 「訂單結束之後我要離開電腦然後走向工廠，然後手機才響拿起來核准…回到電腦讓我繼續 demo」.)
// The 3D's auto driver (factory3d/useGuidedDemo.ts) sets it every tick from the timeline (demo/cues.ts advance(…, auto)):
// beatOf() maps the cue (auto.ts AUTO_CUES), whether the lead has reached the desk and whether the cue's hold is over
// to a beat. null outside the auto demo (the manual ▶ Demo and normal use never set one). The ids key the prompter's
// lines (script.ts) and the captions (auto.ts AUTO_TEXT): change all three together.
import { useSyncExternalStore } from 'react'

export type DemoBeat = 'overview' | 'walk' | 'orders' | 'leave' | 'anomaly' | 'phone' | 'repair' | 'robot' | 'back'
export const DEMO_BEATS: readonly DemoBeat[] = ['overview', 'walk', 'orders', 'leave', 'anomaly', 'phone', 'repair', 'robot', 'back']

/** Which auto cue each beat plays on (cues.ts ids). walk, orders and leave share the 急單 cue: walk until the lead is at
 *  the desk, orders while the order is resolved there, leave once that hold is over (the lead heads out to 7 號機). */
export const BEAT_CUE: Record<DemoBeat, string> = {
  overview: 'start', walk: 'rush', orders: 'rush', leave: 'rush', anomaly: 'event', phone: 'h1', repair: 'worker', robot: 'robot', back: 'agent',
}

/** The beat for an auto cue; `atDesk` = the lead has walked to the war-room computer (splits walk / orders);
 *  `released` = the cue's hold is over (the 急單 cue: the order is done, the lead leaves the desk). */
export function beatOf(cueId: string | null | undefined, atDesk = true, released = false): DemoBeat | null {
  switch (cueId) {
    case 'start': return 'overview'
    case 'rush': return released ? 'leave' : atDesk ? 'orders' : 'walk'
    case 'event': return 'anomaly'
    case 'h1': return 'phone'
    case 'worker': return 'repair'
    case 'robot': return 'robot'
    case 'agent': return 'back'
    default: return null
  }
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
