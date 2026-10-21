// The 6-minute guided demo (CJ, 2026-10-22: 「我的3d 工廠要有一個demo鍵 是一個六分鐘的demo流程…大概三分鐘的時候要換到
// Pitch階段提醒我該Pitch嚕」). Pure timeline: the cues in order, when each one shows, and why (a real event, or its time
// fallback). FactoryApp feeds it the elapsed demo time and the live signals; nothing here touches React or three.js,
// so e2e/demo-unit.mjs can drive it with fake clocks.

/** The scenario the demo plays: 「一早訂單湧入」 (branch cj/morning-rush-scenario). One constant, so retiming is one edit. */
export const DEMO_SCENARIO = 'morning_rush'
/** Until morning_rush lands on the server: the plant runs normally, then the 7 號機溫降 scenario loads before its cue. */
export const FALLBACK_LOADS: DemoLoad[] = [
  { at: 0, scenario: 'normal', speed: 10 },
  { at: 62, scenario: 'press7_temp_drop', speed: 10 },
]
/** Demo speed for morning_rush: the story's first half (07:00 → event closed) in about 3 real minutes. */
export const DEMO_SPEED = 30
export const MAIN_LOADS: DemoLoad[] = [{ at: 0, scenario: DEMO_SCENARIO, speed: DEMO_SPEED }]

export const DEMO_TOTAL = 360
export const PITCH_AT = 180
export const WARN_AT = 300

export interface DemoLoad { at: number; scenario: string; speed: number }
/** Live facts a cue can wait for. */
export type Signal = 'orders' | 'event' | 'approval' | 'worker' | 'closed'
export type Signals = Partial<Record<Signal, boolean>>
/** Where the director points the overview camera. */
export type Shot = { kind: 'overview' } | { kind: 'incident'; close?: boolean } | { kind: 'whiteboard' }
export type CueLink = 'console' | 'phone' | 'agent'

export interface Cue {
  id: string
  /** planned time (s from the start) */
  at: number
  title: string
  body: string
  /** advance as soon as this is true (not before `minAt`); else at `at + wait` */
  on?: Signal
  minAt?: number
  wait?: number
  links?: CueLink[]
  shot: Shot
  /** full-screen card with a chime (the pitch switch, the end) */
  big?: boolean
}

export const CUES: Cue[] = [
  { id: 'start', at: 0, title: '一天開始：訂單湧進來', body: '07:00 早班開工，今天的訂單一張張進來：有急單、也有趕不上的單。', shot: { kind: 'overview' } },
  { id: 'console', at: 30, title: '👉 去看主控台', body: '訂單、AI 預先擬好的計劃：趕不上的單已先排好方案。', links: ['console'], shot: { kind: 'overview' } },
  { id: 'event', at: 75, on: 'event', minAt: 50, wait: 10, title: '⚠ 事件：7 號機溫降', body: '偵測器先發現溫度往下掉，才喚醒 AI。巡檢車正開過去。', shot: { kind: 'incident' } },
  { id: 'phone', at: 100, on: 'approval', minAt: 85, wait: 10, title: '📱 手機響了：去核准', body: 'AI 的計劃要主管核准（R2）。拿起手機看方案、按核准。', links: ['phone'], shot: { kind: 'incident', close: true } },
  { id: 'worker', at: 130, on: 'worker', minAt: 112, wait: 8, title: '👷 看員工到現場解決', body: '核准後維修人員走到 7 號機處理，圈圈轉綠就是好了。', shot: { kind: 'incident', close: true } },
  { id: 'agent', at: 160, on: 'closed', minAt: 142, wait: 6, title: '🧠 回頭看 Agent 頁面', body: '思考流程與解決過程：每一步、每個工具、為什麼換假設，全部記在稽核裡。', links: ['agent'], shot: { kind: 'overview' } },
  { id: 'pitch', at: PITCH_AT, title: '🎤 換 Pitch 了！', body: '故事講完，換到白板上的投影片。←／→ 換頁，點白板全螢幕。', big: true, shot: { kind: 'whiteboard' } },
  { id: 'warn', at: WARN_AT, title: '⏱ 剩 1 分鐘', body: '收尾：講結論和要的東西。', shot: { kind: 'whiteboard' } },
  { id: 'end', at: DEMO_TOTAL, title: '結束', body: '謝謝！', big: true, shot: { kind: 'whiteboard' } },
]

export type Reason = 'time' | 'event' | 'skip'
export interface Fired { id: string; t: number; reason: Reason }
export interface Timeline { index: number; fired: Fired[] }
export const startTimeline = (): Timeline => ({ index: 0, fired: [{ id: CUES[0].id, t: 0, reason: 'time' }] })

/** A cue stays up at least this long before an event can replace it. */
const MIN_DWELL = 10

/** When the cue after `tl` fires on its own, given the current time and signals; null = not yet. */
function due(tl: Timeline, t: number, sig: Signals): Reason | null {
  const next = CUES[tl.index + 1]
  if (!next) return null
  const prevAt = tl.fired[tl.fired.length - 1]?.t ?? 0
  if (next.on && sig[next.on] && t >= (next.minAt ?? 0) && t >= prevAt + MIN_DWELL) return 'event'
  if (t >= next.at + (next.on ? next.wait ?? 0 : 0)) return 'time'
  return null
}

/** Advance as far as the time and signals allow (one cue per step, so each shows at least a moment). */
export function step(tl: Timeline, t: number, sig: Signals): Timeline {
  const r = due(tl, t, sig)
  if (!r) return tl
  const i = tl.index + 1
  return { index: i, fired: [...tl.fired, { id: CUES[i].id, t, reason: r }] }
}

/** N (下一步): show the next cue now. Skipping past the pitch cue jumps the clock to it (see skipTime). */
export function skip(tl: Timeline, t: number): Timeline {
  const i = tl.index + 1
  if (!CUES[i]) return tl
  return { index: i, fired: [...tl.fired, { id: CUES[i].id, t, reason: 'skip' }] }
}

/** Skipping into the pitch phase (or beyond) moves the demo clock to that cue, so the 3:00 countdown stays true. */
export function skipTime(tl: Timeline, t: number): number {
  const c = CUES[tl.index]
  return c && c.at >= PITCH_AT ? Math.max(t, c.at) : t
}

export const current = (tl: Timeline) => CUES[tl.index]
export const upcoming = (tl: Timeline): Cue | null => CUES[tl.index + 1] ?? null
export const inPitch = (t: number) => t >= PITCH_AT
export const done = (tl: Timeline) => tl.index === CUES.length - 1

/** Pausable demo clock in real seconds. */
export interface DemoClock { base: number; since: number | null }
export const clockStart = (now: number): DemoClock => ({ base: 0, since: now })
export const clockRead = (c: DemoClock, now: number) => c.base + (c.since == null ? 0 : (now - c.since) / 1000)
export const clockPause = (c: DemoClock, now: number): DemoClock => ({ base: clockRead(c, now), since: null })
export const clockResume = (c: DemoClock, now: number): DemoClock => (c.since == null ? { base: c.base, since: now } : c)
export const clockSet = (c: DemoClock, t: number, now: number): DemoClock => ({ base: t, since: c.since == null ? null : now })

/** m:ss */
export const mss = (s: number) => `${Math.floor(Math.max(0, s) / 60)}:${String(Math.floor(Math.max(0, s) % 60)).padStart(2, '0')}`
