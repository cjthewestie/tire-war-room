// The live facts the demo cues wait for (cues.ts `enter.on`, `hold.until`, `hold.approval`), read from the war room
// state. Shared by whichever screen drives the demo: the 3D factory (factory3d/useGuidedDemo.ts) or the war room
// (demo/useDemo.ts). `seen` remembers the incident the demo is following, so a second event does not move the story,
// and the approvals that were already there when the demo started (a re-run of morning_rush reuses the same ids).
import { primaryEvent, type WarRoomState } from '../store'
import type { Signals } from './cues'

export interface Seen {
  id: string | null
  machine: string | null
  /** approvals granted before the demo (ids), so only this run's count */
  base: Set<string> | null
  /** kept for older callers: how many approvals were granted when the demo armed */
  approved: number | null
}

export const freshSeen = (): Seen => ({ id: null, machine: null, base: null, approved: null })

/** A machine incident (not a plant-wide plan, rush order or supervisor task: those carry machine_id "PLANT"). */
export const isMachine = (machine: string | null | undefined) => !!machine && machine !== 'PLANT'

const isGranted = (status: string) => status === 'approved' || status === 'modified'
const keyOf = (r: { id?: string; event_id: string }) => r.id ?? r.event_id
/** The orders beat's task: morning_rush's rush order EV-…-RUSH, or the auto demo's 今日訂單排程 (早班開工, EV-…-PLAN). */
export const isOrderTask = (eventId: string | null | undefined) => !!eventId && /-(RUSH|PLAN)(-\d+)?$/.test(eventId)
const isRush = isOrderTask
/** a supervisor command (the SO-012 pre-plan) is EV-…-CMD */
const isCmd = (eventId: string | null | undefined) => !!eventId && eventId.endsWith('-CMD')

export function signals(state: WarRoomState, seen: Seen): Signals {
  const events = state.snapshot?.events ?? []
  const open = events.filter((e) => e.state !== 'closed' && e.state !== 'safe_stop' && isMachine(e.machine_id))
  const ev = primaryEvent(open)
  if (ev && !seen.id) { seen.id = ev.id; seen.machine = ev.machine_id }
  seen.base ??= new Set(state.resolved.filter((r) => isGranted(r.status)).map(keyOf))
  seen.approved ??= seen.base.size
  const fresh = state.resolved.filter((r) => isGranted(r.status) && !seen.base!.has(keyOf(r)))
  const followed = seen.id ? events.find((e) => e.id === seen.id) ?? null : null
  const workers = state.world?.people.workers ?? []
  const onTask = !!seen.machine && workers.some((w) => w.machine_id === seen.machine && (w.state === '前往中' || w.state === '執行中'))
  // the story follows the incident: its own approval card, its own approval (an order plan approved meanwhile is not it)
  const mine = fresh.filter((r) => r.event_id === seen.id)
  const approved = !!seen.id && mine.length > 0
  // H1 → H2 (CJ, 2026-10-22: the demo shows 「AI 猜錯換假設」): the program's check rejects H1, then a second card
  const rejected = !!followed && (followed.hypotheses ?? []).some((h) => h.status === 'rejected')
  const pendingHere = !!seen.id && state.pending.some((p) => p.event_id === seen.id)
  const tasks = seen.id ? (state.world?.tasks.items ?? []).filter((k) => k.event_id === seen.id) : []
  const closed = !!seen.id && (!followed || followed.state === 'closed')
  const cmd = events.filter((e) => isCmd(e.id))
  const tile = seen.machine ? (state.snapshot?.machines ?? []).find((m) => m.machine_id === seen.machine) : undefined
  return {
    orders: (state.world?.orders.items.length ?? 0) > 0,
    rushCard: state.pending.some((p) => isRush(p.event_id)) || fresh.some((r) => isRush(r.event_id)),
    rushApproved: fresh.some((r) => isRush(r.event_id)),
    // auto-play's 訂單 beat: this run's rush plan approved and its event done (plan → approved → done)
    rushRunning: fresh.some((r) => isRush(r.event_id) && events.some((e) => e.id === r.event_id
      && (e.state === 'execute' || e.state === 'verify' || e.state === 'closed' || e.state === 'safe_stop'))),
    rushDone: fresh.some((r) => isRush(r.event_id) && events.some((e) => e.id === r.event_id && (e.state === 'closed' || e.state === 'safe_stop'))),
    // the SO-012 pre-plan (a supervisor command event): in and done
    preplan: cmd.length > 0 && cmd.every((e) => e.state === 'closed' || e.state === 'safe_stop'),
    event: !!ev,
    approval: pendingHere,
    approved,
    rejected,
    approval2: rejected && pendingHere,
    approved2: rejected && mine.length >= 2,
    worker: approved && (onTask || (!!followed && (followed.state === 'execute' || followed.state === 'verify'))),
    // the crew finished the last repair for this incident: the machine's tile says repaired (「檢修完成・待恢復運轉」),
    // or the event closed with every task done and one task per approved card (H1's 檢修 is done long before H2's
    // 停用檢修, so 「all done」 alone would fire early). Never on status running: 7 號機 stays 停用 until 恢復運轉.
    workerDone: approved && (!!tile?.repaired || (closed && tasks.length >= mine.length && tasks.every((k) => k.status === '完成'))),
    closed,
    // auto-play's 維修 beat: the crew's repair done, or (the mock: no crew) the incident closed with the press running again
    repaired: approved && (!!tile?.repaired || (closed && tasks.length >= mine.length && tasks.every((k) => k.status === '完成'))
      || (closed && tile?.status === 'running')),
    crewDone: approved && ((tasks.length > 0 && tasks.every((k) => k.status === '完成')) || !!tile?.repaired || closed),
    // the walk to 7 號機 starts as soon as the last repair is approved (the crew works while the lead runs there)
    fixing: (rejected && mine.length >= 2) || (approved && (!!tile?.repaired || closed)),
  }
}
