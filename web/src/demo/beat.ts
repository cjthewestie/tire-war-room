// The auto-play demo's current beat (/factory?demo=cure07, the real backend; ?mock=1 as the fallback): one small, stable
// API for anything that narrates the run (the presenter's prompter). CJ's flow, in order:
//   overview 俯瞰 → walk 走向電腦 → orders 訂單任務進來然後解決 → anomaly 轉頭有異常事件 → approve 按手機核准 →
//   repair 員工跑去解決 → robot 跟巡檢機器人互動 → warroom 回到戰情室（自動播放到此結束）
// The 3D's auto driver (factory3d/useGuidedDemo.ts) sets it every tick from the timeline (demo/cues.ts advance(…, auto)):
// beatOf() maps the cue (auto.ts AUTO_CUES) and whether the lead has reached the desk to a beat. null outside the auto
// demo (the manual ▶ Demo and normal use never set one). The beat ids are a fixed contract: add, never rename.
import { useSyncExternalStore } from 'react'

export type DemoBeat = 'overview' | 'walk' | 'orders' | 'anomaly' | 'approve' | 'repair' | 'robot' | 'warroom'
export const DEMO_BEATS: readonly DemoBeat[] = ['overview', 'walk', 'orders', 'anomaly', 'approve', 'repair', 'robot', 'warroom']

/** Which auto cue each beat plays on (cues.ts ids). walk and orders share the 急單 cue: walk until the lead is at the desk. */
export const BEAT_CUE: Record<DemoBeat, string> = {
  overview: 'start', walk: 'rush', orders: 'rush', anomaly: 'event', approve: 'h1', repair: 'worker', robot: 'robot', warroom: 'agent',
}

/** The beat for an auto cue; `atDesk` = the lead has walked to the war-room computer (splits walk / orders). */
export function beatOf(cueId: string | null | undefined, atDesk = true): DemoBeat | null {
  switch (cueId) {
    case 'start': return 'overview'
    case 'rush': return atDesk ? 'orders' : 'walk'
    case 'event': return 'anomaly'
    case 'h1': return 'approve'
    case 'worker': return 'repair'
    case 'robot': return 'robot'
    case 'agent': return 'warroom'
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
