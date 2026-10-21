// The live facts the demo cues wait for (cues.ts `on`), read from the war room state. Shared by whichever screen drives
// the demo: the 3D factory (factory3d/useGuidedDemo.ts) or the war room (demo/useDemo.ts). `seen` remembers the
// incident the demo is following, so a second event does not move the story.
import { primaryEvent, type WarRoomState } from '../store'
import type { Signals } from './cues'

export interface Seen { id: string | null; machine: string | null; approved: number | null }

export const freshSeen = (): Seen => ({ id: null, machine: null, approved: null })

const approvedCount = (s: WarRoomState) => s.resolved.filter((r) => r.status === 'approved' || r.status === 'modified').length

export function signals(state: WarRoomState, seen: Seen): Signals {
  const events = state.snapshot?.events ?? []
  const open = events.filter((e) => e.state !== 'closed' && e.state !== 'safe_stop' && !!e.machine_id)
  const ev = primaryEvent(open)
  if (ev && !seen.id) { seen.id = ev.id; seen.machine = ev.machine_id }
  if (seen.approved == null) seen.approved = approvedCount(state)
  const followed = seen.id ? events.find((e) => e.id === seen.id) ?? null : null
  const workers = state.world?.people.workers ?? []
  const onTask = !!seen.machine && workers.some((w) => w.machine_id === seen.machine && (w.state === '前往中' || w.state === '執行中'))
  return {
    orders: (state.world?.orders.new_today ?? 0) > 0,
    event: !!ev,
    approval: state.pending.length > 0,
    worker: approvedCount(state) > (seen.approved ?? 0) && (onTask || (!!followed && (followed.state === 'execute' || followed.state === 'verify'))),
    closed: !!seen.id && (!followed || followed.state === 'closed'),
  }
}
