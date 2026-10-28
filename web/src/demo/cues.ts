// Shared by the 3D factory (factory3d/useGuidedDemo.ts) and the war room (demo/useDemo.ts): one cue list and one pure
// timeline, so both screens show the same beat (GET/POST /api/demo/state carries the current cue between them).
// CJ, 2026-10-22: 「我的3d 工廠要有一個demo鍵 是一個六分鐘的demo流程…大概三分鐘的時候要換到Pitch階段提醒我該Pitch嚕」,
// 「走動按鍵關鍵處可以讓我按按鍵再繼續」, 「固定視角的意思是幫我安排連貫的視角」.
//
// How a cue runs (advance()): it ENTERS (on a live signal, a delay, or right after the previous one), the camera flies
// there and it HOLDS. A hold ends one of three ways:
//   continue  a presenter checkpoint: 「按 A 繼續」 (A / Space / N). Once the shot has arrived (and its `until` signal is
//             in) the sim is paused while CJ talks (simPaused()), and resumed on continue.
//   approval  waits for the real approval (the sim keeps running); continue is refused (the R2 rule the demo shows).
// Between cues, a cue that enters on a live event (the incident, its card, the crew finishing) waits for it; continue
// cannot fake it, only its timeout can.
//   auto      ends on its own after `min` seconds.
// The story's 3:00 is a soft target: past it the card says 「該換 Pitch 了」; the pitch starts on continue at the last story
// beat (the Agent page, after the robot), or by itself there from 3:30.
// Nothing here touches React or three.js, so e2e/demo-unit.mjs drives it with fake clocks and fake signals.

import { AUTO_CUES, AUTO_FINAL, AUTO_STEPS } from './auto'

/** The scenario the demo plays: 「一早訂單湧入」 (PR #107). One constant, so retiming is one edit. */
export const DEMO_SCENARIO = 'morning_rush'
/** Until morning_rush lands on the server: the plant runs normally, then the 7 號機溫降 scenario loads. */
export const FALLBACK_LOADS: DemoLoad[] = [
  { at: 0, scenario: 'normal', speed: 10 },
  { at: 40, scenario: 'press7_temp_drop', speed: 10 },
]
/** morning_rush's demo speed on main (PR #107, measured live at 40×: 急單 card 0:28, 7 號機 0:50, H1 card 1:09, H1
 *  rejected 1:33, H2 card 1:44, 停用檢修 done 2:01). The demo sets it itself (main has no speed buttons any more). The
 *  checkpoints pause the sim while CJ talks, so the speed only sets how long the plant runs between them. */
export const DEMO_SPEED = 40
export const MAIN_LOADS: DemoLoad[] = [{ at: 0, scenario: DEMO_SCENARIO, speed: DEMO_SPEED }]

/** The story's soft target (「故事目標 3:00」), the automatic pitch, and the deck's length (CJ's slides: 180 s). */
export const STORY_TARGET = 180
export const AUTO_PITCH = 210
export const PITCH_LEN = 180
/** Kept for the war room's 「故事目標」 and older callers. */
export const PITCH_AT = STORY_TARGET
export const DEMO_TOTAL = STORY_TARGET + PITCH_LEN
/** The camera's fly-in (1.0–1.8 s, demoCamera.ts flightTime) plus a beat to settle, before a checkpoint is ready. */
export const ARRIVE = 1.5

export interface DemoLoad { at: number; scenario: string; speed: number }
/** Live facts a cue can wait for (demo/signals.ts). */
export type Signal =
  | 'orders' | 'rushCard' | 'rushApproved' | 'preplan' | 'event' | 'approval' | 'approved' | 'rejected' | 'approval2' | 'approved2'
  | 'worker' | 'workerDone' | 'closed'
  /** auto-play: the rush order's plan approved and its event closed (plan → approved → done); the incident's press
   *  repaired and back (the crew's task done, or the event closed with the press running again) */
  | 'rushDone' | 'repaired'
  /** auto-play's 維修 beat: the crew's tasks for the incident all 完成 (or it is repaired / closed); the temperature
   *  coming back is not waited for */
  | 'crewDone'
  /** auto-play's 訂單 beat: this run's order plan approved and executing (執行中 / 驗證中) or done */
  | 'rushRunning'
  /** the last repair is under way (H2 approved) or done: the walk to 7 號機 starts, the repair is watched, not waited for */
  | 'fixing'
  /** the 3D's own: the walk / camera has arrived and framed the beat (false holds a checkpoint's 「按 A 繼續」) */
  | 'arrived'
export type Signals = Partial<Record<Signal, boolean>>
/** What the camera frames (factory3d/demoCamera.ts). */
export type Shot =
  | { kind: 'establish' } | { kind: 'overview' } | { kind: 'incident'; close?: boolean } | { kind: 'robot' } | { kind: 'whiteboard' }
export type CueLink = 'console' | 'phone' | 'agent'
export type Wait = 'continue' | 'approval' | 'auto'

export interface Cue {
  id: string
  /* `title` is the one wording for the beat on every screen: the 3D cue card and the war room's spotlight caption
     (demo/beats.ts reads it from here), so the two never say different things. */
  /** planned time (s from the start), for the bar's 「下一步」 and the planned-vs-actual table */
  at: number
  title: string
  body: string
  /** when it starts once the previous cue is done: on a signal (or `timeout` s later anyway), or `after` s later;
   *  none = right away */
  enter?: { on?: Signal; timeout?: number; after?: number }
  hold: {
    wait: Wait
    /** seconds after entering before the hold can end (the camera's arrival, a 3 s look at the worker, the robot's
     *  little show); default ARRIVE */
    min?: number
    /** the checkpoint is ready only once this signal is in (or `untilTimeout` s after entering) */
    until?: Signal
    untilTimeout?: number
    /** wait 'approval': the signal that ends it; `card`: if that card never showed after 25 s, continue may pass */
    approval?: Signal
    card?: Signal
    /** a continue hold that ends by itself after this many seconds */
    autoAfter?: number
    /** pause the sim while the checkpoint waits (default: yes for continue) */
    pauseSim?: boolean
  }
  links?: CueLink[]
  shot: Shot
  /** full-screen card with a chime (the pitch switch, the end) */
  big?: boolean
}

export const CUES: Cue[] = [
  { id: 'start', at: 0, title: '一天開始：訂單湧進來', body: '07:00 早班開工，今天的訂單一張張進來：有一般單、有交期很趕的單，還有急單。',
    // ① is ready once the 急單's card is up (the orders flood in meanwhile, the sim runs fast): the plan for it is made
    // during the establishing shot instead of as dead air after it
    hold: { wait: 'continue', min: 4, until: 'rushCard', untilTimeout: 30 }, shot: { kind: 'establish' } },
  { id: 'rush', at: 20, title: '📋 急單進來：先核准', body: '急單 SO-017 到了，AI 已經重排好。去主控台看計劃、按核准。', links: ['console'],
    hold: { wait: 'approval', approval: 'rushApproved', card: 'rushCard' }, shot: { kind: 'overview' } },
  { id: 'console', at: 40, title: '👉 主控台：計劃已先擬好，只等核准或討論', body: '急單排進去後，SO-012 交期有風險：Agent 先擬好保交期的方案，等主管決定。', links: ['console'],
    hold: { wait: 'continue', until: 'preplan', untilTimeout: 25 }, shot: { kind: 'overview' } },
  { id: 'event', at: 75, enter: { on: 'event', timeout: 60 }, title: '⚠ 7 號機溫降：AI 只在規劃時被叫醒', body: '偵測器（不是 AI）先從讀值發現溫度往下掉，才喚醒 AI 規劃。巡檢車也開過去了。',
    hold: { wait: 'continue' }, shot: { kind: 'incident' } },
  { id: 'h1', at: 70, enter: { on: 'approval', timeout: 40 }, title: '📱 核准 H1（第一個假設）', body: 'AI 的第一個假設 H1：蒸汽壓不足，延長硫化就好。修正計劃要主管核准（R2 以上一定要人）。', links: ['phone'],
    hold: { wait: 'approval', approval: 'approved', card: 'approval' }, shot: { kind: 'incident', close: true } },
  { id: 'verify', at: 95, enter: { on: 'rejected', timeout: 60 }, title: '❌ 程式驗證讀值：H1 不成立（119.5°C）', body: '程式比對預期：溫度沒回來，還在往下掉。AI 猜錯了，不是程式放過它，是讀值推翻它。',
    hold: { wait: 'continue' }, shot: { kind: 'incident', close: true } },
  { id: 'h2', at: 105, enter: { on: 'approval2', timeout: 40 }, title: '🔁 AI 換假設 H2：程式驗證失敗才會換', body: '驗證失敗才換假設：H2 是蒸汽洩漏，停用 7 號機檢修、訂單移到別台。再核准一次。', links: ['phone'],
    hold: { wait: 'approval', approval: 'approved2', card: 'approval2' }, shot: { kind: 'incident', close: true } },
  { id: 'worker', at: 120, enter: { on: 'fixing', timeout: 45 }, title: '👷 員工完成維修，程式驗證讀值', body: '核准後維修人員到 7 號機停用檢修，做完圈圈轉綠：「檢修完成・待恢復運轉」。',
    hold: { wait: 'continue', until: 'workerDone', untilTimeout: 25 }, shot: { kind: 'incident', close: true } },
  { id: 'robot', at: 135, title: '🤖 巡檢機器人：只有「規劃」和「換假設」會叫 AI', body: '平常巡檢是程式在做，便宜又不會累；只有規劃和換假設才叫醒 AI。',
    hold: { wait: 'continue', min: 3.2 }, shot: { kind: 'robot' } },
  { id: 'agent', at: 158, title: '🧠 Agent 頁面：每一步都在稽核鏈上', body: '時間軸上看得到：H1 的預期、讀值 119.5°C 推翻它、換成 H2。每一步、每個工具都記在稽核鏈上。', links: ['agent'],
    hold: { wait: 'continue' }, shot: { kind: 'overview' } },
  { id: 'pitch', at: STORY_TARGET, title: '🎤 換 Pitch 了！', body: '故事講完，換到白板上的投影片。LB／RB（←／→）換頁。', big: true,
    hold: { wait: 'continue', autoAfter: 5 }, shot: { kind: 'whiteboard' } },
  { id: 'warn', at: STORY_TARGET + 120, enter: { after: 120 }, title: '⏱ 剩 1 分鐘', body: '收尾：講結論和要的東西。',
    hold: { wait: 'auto', min: 0 }, shot: { kind: 'whiteboard' } },
  { id: 'end', at: DEMO_TOTAL, enter: { after: 60 }, title: '結束・謝謝', body: '謝謝！', big: true,
    hold: { wait: 'continue', pauseSim: false }, shot: { kind: 'whiteboard' } },
]
export const PITCH_INDEX = CUES.findIndex((c) => c.id === 'pitch')
/** The last story beat (the Agent page): from 3:30 it hands over to the pitch by itself. */
export const LAST_STORY_INDEX = PITCH_INDEX - 1
/** The presenter checkpoints (CJ: 「關鍵處可以讓我按按鍵再繼續」), for the badge 「④ / 10」 (the 急單 approval is an
 *  extra stop between ① and ②). */
export const CHECKPOINTS = ['start', 'console', 'event', 'h1', 'verify', 'h2', 'worker', 'robot', 'agent', 'pitch']

export type Reason = 'time' | 'event' | 'continue' | 'auto' | 'skip' | 'follow'
export interface Fired { id: string; t: number; reason: Reason; /** when its hold ended, and how */ out?: number; outBy?: Reason }
export interface Timeline {
  index: number
  enteredAt: number
  released: boolean
  releasedAt: number
  /** demo second the deck started (the pitch checkpoint passed; null = not yet) */
  pitchAt: number | null
  /** auto-play: when the current hold's `until` signal first came in (for its `untilDwell`) */
  untilAt?: number | null
  fired: Fired[]
}
export const startTimeline = (): Timeline => ({ index: 0, enteredAt: 0, released: false, releasedAt: 0, pitchAt: null, fired: [{ id: CUES[0].id, t: 0, reason: 'time' }] })

/** Signals stay true once seen (the approval card is gone once approved; the cue should still follow it). */
export function latch(seen: Signals, now: Signals): Signals {
  const out: Signals = { ...seen }
  for (const k of Object.keys(now) as Signal[]) if (now[k]) out[k] = true
  return out
}

function enter(tl: Timeline, i: number, t: number, reason: Reason): Timeline {
  return {
    ...tl, index: i, enteredAt: t, released: false, releasedAt: t, untilAt: null,
    fired: [...tl.fired, { id: CUES[i].id, t, reason }],
  }
}
function release(tl: Timeline, t: number, by: Reason): Timeline {
  const fired = tl.fired.slice()
  fired[fired.length - 1] = { ...fired[fired.length - 1], out: t, outBy: by }
  return { ...tl, released: true, releasedAt: t, fired, pitchAt: tl.pitchAt ?? (tl.index === PITCH_INDEX ? t : null) }
}

/** Is the current checkpoint ready (camera arrived, its `until` in)? */
export function ready(tl: Timeline, t: number, sig: Signals): boolean {
  const h = CUES[tl.index].hold
  const e = t - tl.enteredAt
  return e >= (h.min ?? ARRIVE) && sig.arrived !== false && (!h.until || !!sig[h.until] || e >= (h.untilTimeout ?? Infinity))
}

/** May continue end this hold now? (false at an approval checkpoint until the approval, with one exception: the card
 *  it waits for never showed — a model that failed — after 25 s.) */
export function canContinue(tl: Timeline, t: number, sig: Signals): boolean {
  if (tl.released) return true
  const h = CUES[tl.index].hold
  if (h.wait !== 'approval') return true
  return !!(h.card && !sig[h.card] && t - tl.enteredAt >= 25)
}

export interface Step { tl: Timeline; refused?: boolean }

/** 自動播放（?demo=cure07）的時間軸：不等任何人按鍵（核准除外：真實後端要人按，Mock 由 Demo 按）；只走 AUTO_CUES，
 *  每一步怎麼進、怎麼停看 auto.ts AUTO_STEPS；最後一步（AUTO_FINAL，回到戰情室）停住，不往 Pitch 走。 */
const nextAuto = (i: number) => { let k = i + 1; while (CUES[k] && !AUTO_CUES.includes(CUES[k].id)) k++; return k }

/** Is the auto step's hold over? At least `min` s; then the walk has arrived and its `until` is in (or the card it waits
 *  for never showed in `cardTimeout` s), or its `timeout` passed anyway. */
export function autoHoldDone(tl: Timeline, t: number, sig: Signals): boolean {
  const h = AUTO_STEPS[CUES[tl.index].id]?.hold ?? { min: ARRIVE }
  const e = t - tl.enteredAt
  if (e < h.min) return false
  if (h.timeout != null && e >= h.timeout) return true
  if (h.needArrive && sig.arrived === false) return false
  if (h.until && sig[h.until] && h.untilDwell) return t - (tl.untilAt ?? t) >= h.untilDwell
  if (!h.until || sig[h.until]) return true
  // the card never showed (cardTimeout s; 0 = the scenario has none: do not wait for it)
  return !!h.card && !sig[h.card] && e >= (h.cardTimeout ?? Infinity)
}

function advanceAuto(tl: Timeline, t: number, sig: Signals, skip = false): Step {
  const cur = CUES[tl.index]
  // 「下一階段 ▶」 (CJ 10-22): end this hold now, or start the next step now (no waiting for its event)
  if (skip) {
    if (!tl.released) return cur.id === AUTO_FINAL ? { tl } : { tl: release(tl, t, 'continue') }
    const k = nextAuto(tl.index)
    return CUES[k] ? { tl: enter(tl, k, t, 'continue') } : { tl }
  }
  if (!tl.released) {
    if (cur.id === AUTO_FINAL) return { tl }
    const u = AUTO_STEPS[cur.id]?.hold.until
    if (u && sig[u] && tl.untilAt == null) tl = { ...tl, untilAt: t }
    return autoHoldDone(tl, t, sig) ? { tl: release(tl, t, 'auto') } : { tl }
  }
  const i = nextAuto(tl.index)
  const next = CUES[i]
  if (!next) return { tl }
  const en = AUTO_STEPS[next.id]?.enter, since = t - tl.releasedAt
  if (en) {
    if (en.min != null && since < en.min) return { tl }
    if (sig[en.on]) return { tl: enter(tl, i, t, 'event') }
    return since >= en.timeout ? { tl: enter(tl, i, t, 'time') } : { tl }
  }
  return { tl: enter(tl, i, t, 'auto') }
}

/** One tick: `cont` = continue pressed since the last tick (A / Space / N, or the server's cue moved on); `auto` = the
 *  auto-play timeline (advanceAuto). */
export function advance(tl: Timeline, t: number, sig: Signals, cont = false, auto = false, skip = false): Step {
  if (auto) return advanceAuto(tl, t, sig, skip)
  // the soft target: from 3:30 the last story beat (the Agent page) hands over to the pitch by itself; earlier beats only get the nudge
  // (CJ paces the story; a beat is never cut short)
  if (tl.index === LAST_STORY_INDEX && t >= AUTO_PITCH) return { tl: enter(tl, PITCH_INDEX, t, 'time') }
  const cur = CUES[tl.index]
  if (!tl.released) {
    const h = cur.hold, e = t - tl.enteredAt
    if (h.wait === 'approval') {
      if (e >= (h.min ?? ARRIVE) && h.approval && sig[h.approval]) return { tl: release(tl, t, 'event') }
      if (cont) return canContinue(tl, t, sig) ? { tl: release(tl, t, 'continue') } : { tl, refused: true }
      return { tl }
    }
    // still walking / framing the beat: continue waits for the arrival (CJ presses when the picture is there)
    if (cont) return sig.arrived === false ? { tl, refused: true } : { tl: release(tl, t, 'continue') }
    if (h.wait === 'auto' && ready(tl, t, sig)) return { tl: release(tl, t, 'auto') }
    if (h.autoAfter != null && e >= h.autoAfter) return { tl: release(tl, t, 'auto') }
    return { tl }
  }
  const i = tl.index + 1
  const next = CUES[i]
  if (!next) return { tl }
  const en = next.enter
  const since = t - tl.releasedAt
  // waiting for a live event (the incident, its card, the crew finishing): continue cannot fake it, only the timeout can
  if (cont) return en?.on ? { tl, refused: true } : { tl: enter(tl, i, t, 'skip') }
  if (!en) return { tl: enter(tl, i, t, tl.fired[tl.fired.length - 1].outBy === 'continue' ? 'continue' : 'auto') }
  if (en.on && sig[en.on]) return { tl: enter(tl, i, t, 'event') }
  if (en.after != null && since >= en.after) return { tl: enter(tl, i, t, 'time') }
  if (en.timeout != null && since >= en.timeout) return { tl: enter(tl, i, t, 'time') }
  return { tl }
}

/** What the beat is waiting for (posted to /api/demo/state): CJ's continue, the real approval, the next live event,
 *  or nothing (the camera is still flying in / an automatic beat). */
export function waiting(tl: Timeline, t: number, sig: Signals): 'continue' | 'approval' | 'event' | null {
  if (tl.released) return CUES[tl.index + 1] ? 'event' : null
  const h = CUES[tl.index].hold
  if (h.wait === 'approval') return canContinue(tl, t, sig) && h.card && !sig[h.card] ? 'continue' : 'approval'
  if (h.wait === 'continue') return ready(tl, t, sig) ? 'continue' : null
  return null
}

/** Is the timeline waiting on the plant (not on CJ)? Then the driver runs the sim fast (BOOST_SPEED), so a wait
 *  the presenter did not ask for stays short (CJ, 2026-10-22: 「demo 模式整個不順 有些地方花的時間太長了」):
 *  the next beat's event, a hold's `until`, or an approval card that has not shown up yet. */
export function plantWait(tl: Timeline, sig: Signals): boolean {
  if (tl.released) { const en = CUES[tl.index + 1]?.enter; return !!en?.on && !sig[en.on] }
  const h = CUES[tl.index].hold
  if (h.until && !sig[h.until]) return true
  return h.wait === 'approval' && !!h.card && !sig[h.card]
}
export const BOOST_SPEED = 300

/** Should the sim be paused now? Only while CJ talks at a ready continue checkpoint (never at the approval: the
 *  approval and the repair need the plant running). */
export function simPaused(tl: Timeline, t: number, sig: Signals): boolean {
  if (tl.released) return false
  const h = CUES[tl.index].hold
  return h.wait === 'continue' && h.pauseSim !== false && ready(tl, t, sig)
}

export const current = (tl: Timeline) => CUES[tl.index]
export const upcoming = (tl: Timeline): Cue | null => CUES[tl.index + 1] ?? null
export const inPitch = (tl: Timeline) => tl.index >= PITCH_INDEX
export const done = (tl: Timeline) => tl.index === CUES.length - 1
/** Past the story's soft target and still telling the story: 「該換 Pitch 了」. */
export const overTarget = (tl: Timeline, t: number) => tl.index < PITCH_INDEX && t >= STORY_TARGET
/** Seconds left in the pitch (the whole deck until it starts; null in the story). */
export const pitchLeft = (tl: Timeline, t: number) => (tl.index < PITCH_INDEX ? null : tl.pitchAt == null ? PITCH_LEN : PITCH_LEN - (t - tl.pitchAt))

/** Pausable demo clock in real seconds (the presenter's pause; checkpoints don't stop it). */
export interface DemoClock { base: number; since: number | null }
export const clockStart = (now: number): DemoClock => ({ base: 0, since: now })
export const clockRead = (c: DemoClock, now: number) => c.base + (c.since == null ? 0 : (now - c.since) / 1000)
export const clockPause = (c: DemoClock, now: number): DemoClock => ({ base: clockRead(c, now), since: null })
export const clockResume = (c: DemoClock, now: number): DemoClock => (c.since == null ? { base: c.base, since: now } : c)

/** m:ss */
export const mss = (s: number) => `${Math.floor(Math.max(0, s) / 60)}:${String(Math.floor(Math.max(0, s) % 60)).padStart(2, '0')}`
