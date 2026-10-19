// Client state: one reducer fed by WsMessages (from the live /ws or from the mock engine).
import type {
  ApprovalRequest, AuditEntry, DashboardSnapshot, Event, EventState, ScheduleVersion, WorldDelta, WorldSnapshot, WsMessage,
} from './contracts'

export interface ClockState { factory_time: string; speed: number; paused: boolean }

export interface WarRoomState {
  snapshot: DashboardSnapshot | null
  pending: ApprovalRequest[]
  resolved: ApprovalRequest[]
  clock: ClockState | null
  schedule: ScheduleVersion | null
  scheduleRev: number          // bumps on every schedule message so views refetch versions
  audit: AuditEntry[]
  visited: Record<string, EventState[]>   // per event id, states seen in order
  lastAlert: { event_id: string; title: string; summary: string; ts: string } | null
  lastMessageAt: number
  /** The whole plant around the machines (orders, materials, dock, people, energy, quality, maintenance). Live only. */
  world: WorldSnapshot | null
}

export const initialState: WarRoomState = {
  snapshot: null, pending: [], resolved: [], clock: null, schedule: null, scheduleRev: 0,
  audit: [], visited: {}, lastAlert: null, lastMessageAt: 0, world: null,
}

/** Apply a WS "world" message: a full one replaces, a delta overwrites only the sections it carries.
 *  A delta before any full snapshot is ignored (GET /api/world or the on-connect full message fills it). */
export function mergeWorld(w: WorldSnapshot | null, d: WorldDelta): WorldSnapshot | null {
  const { full, ...rest } = d
  if (full) return rest as WorldSnapshot
  if (!w || d.rev < w.rev) return w
  return { ...w, ...rest }
}

function upsertEvent(list: Event[], e: Event): Event[] {
  const i = list.findIndex((x) => x.id === e.id)
  if (i < 0) return [e, ...list]
  const copy = list.slice()
  copy[i] = e
  return copy
}

function trackVisited(v: Record<string, EventState[]>, events: Event[]): Record<string, EventState[]> {
  let out = v
  for (const e of events) {
    const seen = out[e.id] ?? []
    if (seen[seen.length - 1] !== e.state) {
      if (out === v) out = { ...v }
      out[e.id] = [...seen, e.state]
    }
  }
  return out
}

export type Action = { kind: 'ws'; msg: WsMessage } | { kind: 'reset' }

export function reduce(s: WarRoomState, a: Action): WarRoomState {
  if (a.kind === 'reset') return initialState
  const msg = a.msg
  const base = { ...s, lastMessageAt: Date.now() }
  switch (msg.type) {
    case 'snapshot':
      return {
        ...base,
        snapshot: msg.data,
        pending: msg.data.pending_approvals,
        visited: trackVisited(s.visited, msg.data.events),
      }
    case 'machines': {
      if (!s.snapshot) return base
      const machines = msg.data.machines
      const abnormal = machines.filter((m) => isAbnormal(m.status)).length
      return {
        ...base,
        snapshot: {
          ...s.snapshot, machines,
          header: { ...s.snapshot.header, abnormal_count: abnormal, normal_count: machines.length - abnormal },
        },
      }
    }
    case 'event':
      return {
        ...base,
        snapshot: s.snapshot ? { ...s.snapshot, events: upsertEvent(s.snapshot.events, msg.data) } : s.snapshot,
        visited: trackVisited(s.visited, [msg.data]),
      }
    case 'safety_action':
      return s.snapshot
        ? { ...base, snapshot: { ...s.snapshot, safety_actions: [msg.data, ...s.snapshot.safety_actions.filter((x) => x.id !== msg.data.id)] } }
        : base
    case 'alert_card':
      return { ...base, lastAlert: { event_id: msg.data.event_id, ...msg.data.card, ts: msg.ts } }
    case 'approval_request':
      return { ...base, pending: [...s.pending.filter((p) => p.id !== msg.data.id), msg.data] }
    case 'approval_resolved':
      return {
        ...base,
        pending: s.pending.filter((p) => p.id !== msg.data.id),
        resolved: [msg.data, ...s.resolved.filter((p) => p.id !== msg.data.id)].slice(0, 20),
      }
    case 'schedule':
      return { ...base, schedule: msg.data, scheduleRev: s.scheduleRev + 1 }
    case 'audit':
      return { ...base, audit: [...s.audit, msg.data].slice(-300) }
    case 'clock':
      return {
        ...base,
        clock: msg.data,
        snapshot: s.snapshot ? { ...s.snapshot, header: { ...s.snapshot.header, factory_time: msg.data.factory_time } } : s.snapshot,
      }
    case 'world':
      return { ...base, world: mergeWorld(s.world, msg.data) }
    case 'anomaly':
      return base
    default:
      return base          // a message type this build does not know yet: keep the state
  }
}

export function isAbnormal(status: string): boolean {
  return status === 'alarm' || status === 'warning' || status === 'stopped' || status === 'out_of_service'
}

/** Most relevant event: open ones first (newest), then the latest closed. */
export function primaryEvent(events: Event[] | undefined): Event | null {
  if (!events || events.length === 0) return null
  const open = events.filter((e) => e.state !== 'closed' && e.state !== 'safe_stop')
  const pool = open.length ? open : events
  return pool.slice().sort((a, b) => (a.opened_at < b.opened_at ? 1 : -1))[0]
}
