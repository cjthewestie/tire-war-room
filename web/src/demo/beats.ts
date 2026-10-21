// The war room's half of the guided demo (CJ, 2026-10-22: 「戰情室也要幫我弄一個demo模式」): for each shared cue
// (cues.ts, the same ids the 3D factory shows), which page the war room opens, which element it spotlights and the short
// caption next to it. Pure data and pure lookups, so tests/test_demo_beats (e2e/demo-beats-unit.mjs) can check the map
// without a browser. Anchors are CSS selectors tried in order; the first one on screen gets the spotlight, none = caption only.
import type { Event } from '../contracts'
import type { Tab } from '../components/Header'
import type { WarRoomState } from '../store'
import { isMachine } from './signals'
import type { DemoWaiting } from './state'

/** Which event a beat is about: the incident (7 號機), the pre-planned order item, or none. */
export type BeatFocus = 'incident' | 'order' | null

export interface Beat {
  tab: Tab
  /** CSS selectors, first match on screen wins. `{id}` is replaced by the focused event's id, `{machine}` by its
   *  machine; a leading `?` keeps the selector only once the focused event exists (the 主控台 plan area shows whatever
   *  is selected, so before the incident it would spotlight an order plan); `a & b` spotlights both. */
  anchors: string[]
  caption: string
  focus: BeatFocus
  /** the calm full-screen card while CJ pitches on the 3D whiteboard */
  pitch?: boolean
}

export const BEATS: Record<string, Beat> = {
  start: {
    tab: 'agent', focus: null, caption: '訂單湧進來',
    anchors: ['[data-testid="home-overview"]', '[data-testid="approval-list"]', '[data-testid="status-strip"]'],
  },
  console: {
    tab: 'agent', focus: 'order', caption: '計劃已經先擬好，只等核准或討論',
    anchors: ['[data-testid="home-item-{id}"]', '[data-testid="approval-list"]', '[data-testid="plan-area"]'],
  },
  event: {
    tab: 'agent', focus: 'incident', caption: 'AI 只在規劃時被叫醒',
    anchors: ['?[data-testid="plan-area"]', '[data-testid="home-item-{id}"]', '[data-testid="status-strip"]'],
  },
  phone: {
    tab: 'agent', focus: 'incident', caption: '📱 主管手機核准（R2 以上一定要人）',
    anchors: ['?[data-demo-anchor="approve-bar"]', '?[data-testid="plan-area"]', '[data-testid="home-item-{id}"]', '[data-testid="status-strip"]'],
  },
  worker: {
    tab: 'agent', focus: 'incident', caption: '員工完成，程式驗證讀值',
    anchors: ['?[data-testid="result-strip"]', '?[data-testid="plan-area"]', '[data-testid="home-item-{id}"]'],
  },
  agent: {
    tab: 'console', focus: 'incident', caption: '每一步都在稽核鏈上',
    anchors: ['[data-testid="trace-body"] & [data-testid="tasklog"]', '[data-testid="trace-body"]', '[data-testid="agent-page"]'],
  },
  robot: {
    tab: 'sm', focus: null, caption: '只有「規劃」和「換假設」會叫 AI',
    anchors: ['[data-testid="sm-stages"]', '[data-testid="sm-board"]', '[data-testid="sm-page"]'],
  },
  pitch: { tab: 'agent', focus: null, caption: 'Pitch 中', anchors: [], pitch: true },
  warn: { tab: 'agent', focus: null, caption: 'Pitch 中・剩 1 分鐘', anchors: [], pitch: true },
  end: { tab: 'agent', focus: null, caption: '謝謝', anchors: [], pitch: true },
}

/** An unknown cue (a new one in cues.ts the war room has no beat for yet): stay on 主控台, caption only. */
export function beatFor(cueId: string | null | undefined): Beat | null {
  if (!cueId) return null
  return BEATS[cueId] ?? { tab: 'agent', focus: null, caption: '', anchors: [] }
}

const isOpen = (e: Event) => e.state !== 'closed' && e.state !== 'safe_stop'

/** The event a beat is about. The incident = the newest machine event (open first); the order item = the newest
 *  plant-wide event (machine "PLANT" or none: the pre-planned day plan, a rush-order plan). */
export function focusEvent(focus: BeatFocus, events: Event[] | undefined): Event | null {
  if (!focus || !events?.length) return null
  const pool = events.filter((e) => (focus === 'incident' ? isMachine(e.machine_id) : !isMachine(e.machine_id)))
  if (!pool.length) return null
  const open = pool.filter(isOpen)
  return (open.length ? open : pool).slice().sort((a, b) => (a.opened_at < b.opened_at ? 1 : -1))[0]
}

/** The beat's selectors with the focused event filled in; a selector that needs an event it does not have is dropped. */
export function anchorsFor(beat: Beat, ev: Event | null): string[] {
  return beat.anchors.flatMap((a) => {
    const guarded = a.startsWith('?')
    if (guarded) a = a.slice(1)
    if (!a.includes('{') && !guarded) return [a]
    if (!ev) return []
    return [a.replace('{id}', ev.id).replace('{machine}', ev.machine_id ?? '')]
  })
}

/** What the beat waits for before the next one: the phone beat needs the real approval (never skippable), a beat
 *  whose next cue follows a plant fact waits for the event, the rest go on the presenter's 繼續 or the clock. */
export function waitingFor(cueId: string, nextOn: string | undefined): DemoWaiting {
  if (cueId === 'phone') return 'approval'
  if (nextOn) return 'event'
  return null
}

/** The war room's state for the beat, for tests and the e2e hook. */
export function beatView(cueId: string | null, state: Pick<WarRoomState, 'snapshot'>) {
  const beat = beatFor(cueId)
  if (!beat) return null
  const ev = focusEvent(beat.focus, state.snapshot?.events)
  return { tab: beat.tab, anchors: anchorsFor(beat, ev), caption: beat.caption, eventId: ev?.id ?? null, pitch: !!beat.pitch }
}
