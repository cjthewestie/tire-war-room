// The event state machine, mirrored from warroom/contracts.py (EventState, ALLOWED_TRANSITIONS,
// MAX_CONSECUTIVE_FAILURES). tests/test_state_graph.py checks this file against Python, so keep the two in sync.
import type { EventState } from '../contracts'
import type { Item } from './transcript'

export const STATES: EventState[] = ['detected', 'classify', 'plan', 'simulate', 'gate', 'await_approval', 'execute', 'verify', 'replan', 'closed', 'safe_stop']

export const ALLOWED_TRANSITIONS: Record<EventState, EventState[]> = {
  detected: ['classify', 'safe_stop'],
  classify: ['plan', 'closed', 'safe_stop'],
  plan: ['simulate', 'safe_stop'],
  simulate: ['gate', 'replan', 'safe_stop'],
  gate: ['await_approval', 'execute', 'replan', 'safe_stop'],
  await_approval: ['execute', 'replan', 'safe_stop'],
  execute: ['verify', 'await_approval', 'safe_stop'],
  verify: ['closed', 'replan', 'safe_stop'],
  replan: ['simulate', 'safe_stop'],
  closed: [],
  safe_stop: [],
}

export const MAX_CONSECUTIVE_FAILURES = 2

/** Upper-case names as in warroom/contracts.py, shown next to the Chinese label. */
export const STATE_CODE: Record<EventState, string> = Object.fromEntries(STATES.map((s) => [s, s.toUpperCase()])) as Record<EventState, string>

export interface Hop { from: EventState | null; to: EventState; ts: string }

/** The path an event took, from its audit 'state' rows (oldest first). */
export function pathOf(items: Item[]): Hop[] {
  const out: Hop[] = []
  for (const i of items) if (i.k === 'state' && i.to) out.push({ from: (i.from ?? null) as EventState | null, to: i.to as EventState, ts: i.ts })
  return out
}

/** For each transcript item, the state the event was in when it was written (by walking the state rows). */
export function stateAt(items: Item[]): (EventState | null)[] {
  let cur: EventState | null = null
  return items.map((i) => { if (i.k === 'state' && i.to) cur = i.to as EventState; return cur })
}
