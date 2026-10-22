// What the progress ring above one machine shows (issue #36): pure, shared by MachineRings, the focus tag and the e2e hook.
// Priority: a worker task there (暖機 / 檢修 / 補料 ..., issue #17), maintenance in progress, alarm / stopped / out of
// service, the running batch or cure (MachineTile.step_progress), else idle.
// Fast repair (Kyle 10-21): a repair task (檢修／停用檢修／放慢投料／暫停投料) on the machine wins over other tasks, its ring
// follows the clock between world steps (a 3-second repair would otherwise jump 0 → done), and a just-finished repair
// shows a full green ring for a few seconds.
// Kyle 10-21 flow: after the repair (Event.repaired_at) the readings ramp back over RECOVER_FAST_S wall seconds: an amber
// 回溫中 ring fills over that time, then 已恢復 (green) for a few seconds once the event recovered (recovered_at / closed).
import type { Event, MachineTile, WorkTask, WorldSnapshot } from '../contracts'
import { REPAIR_KINDS, expectationOf, recoverWord } from '../aiStatus'
import { LIGHT_HEX, WORK_HEX } from './layout'

export type RingKind = 'task' | 'recover' | 'maint' | 'alarm' | 'batch' | 'idle'
export interface MachineRing { kind: RingKind; progress: number; color: string; label: string }
/** A machine's fast-repair recovery (ringRecoveries): readings coming back (frac of the ramp so far), or recovered `ageS`
 *  wall seconds ago. */
export type RingRecovery = { phase: 'recovering'; frac: number; label: string } | { phase: 'recovered'; ageS: number }

const TASK_COLOR: Record<string, string> = {
  暖機: '#ff8a50', 檢修: '#b39ddb', 停用檢修: '#b39ddb', 品管: '#b39ddb',
  補料: WORK_HEX, 調料: WORK_HEX, 換模準備: WORK_HEX, 盤點: WORK_HEX,
}
const ALARM = new Set(['alarm', 'stopped', 'out_of_service'])
const STATUS_ZH: Record<string, string> = { alarm: '異常', stopped: '停機', out_of_service: '停用', warning: '注意' }
const DONE_GREEN = LIGHT_HEX.green      // the status lamp's green
const DONE_HOLD_WALL_S = 6        // a finished repair stays green this long (wall seconds; at least one factory minute)
const DONE_GRACE_WALL_S = 2       // a finished repair over a machine still red stays at 100% this long (the fix landing)
const RECOVER_AMBER = LIGHT_HEX.amber   // the status lamp's amber (WARNING) while the readings come back
const clamp = (x: number) => Math.max(0, Math.min(1, x))

/** Where an event's machine is after its repair: readings coming back, or recovered (null: not a repaired event). */
export function recoveryPhase(ev: Event): 'recovering' | 'recovered' | null {
  if (!ev.repaired_at || ev.state === 'safe_stop') return null
  return ev.recovered_at || ev.state === 'closed' ? 'recovered' : 'recovering'
}

/** Per machine, the newest repaired event's recovery. `seen`: wall ms each phase was first seen (the ramp and the green
 *  hold run on the wall clock, the same as the server's ramp: another event's model call can hold the plant clock);
 *  `wall`: now; a recovery older than the hold in factory time is history (a page opened later shows no green). */
export function ringRecoveries(events: Event[], seen: Record<string, number>, wall: number, recoverS: number, now: number, speed = 1): Record<string, RingRecovery> {
  const out: Record<string, RingRecovery> = {}
  const hold = Math.max(60_000, DONE_HOLD_WALL_S * 1000 * speed)
  for (const ev of [...events].sort((a, b) => ((a.repaired_at ?? '') < (b.repaired_at ?? '') ? -1 : 1))) {
    const ph = recoveryPhase(ev)
    if (!ph) continue
    const t0 = seen[recoveryKey(ev, ph)] ?? wall      // not recorded yet: seen just now
    if (ph === 'recovering') {
      out[ev.machine_id] = { phase: 'recovering', frac: Math.min(0.95, Math.max(0.03, (wall - t0) / (Math.max(recoverS, 0.5) * 1000))), label: recoverWord(expectationOf(ev)).replace(/^溫度回(升|降)中$/, '回溫中') }
    } else {
      const back = Date.parse(ev.recovered_at ?? ev.closed_at ?? '')
      if (Number.isFinite(back) && now - back > hold) { delete out[ev.machine_id]; continue }
      out[ev.machine_id] = { phase: 'recovered', ageS: (wall - t0) / 1000 }
    }
  }
  return out
}
export const recoveryKey = (ev: Event, ph: 'recovering' | 'recovered') => `${ev.id}|${ev.repaired_at}|${ph}`

/** The task's progress now: the server's (as of the last world step), or where the clock is between assignment and its
 *  ETA, kept just under done until the server says 完成. */
function liveProgress(t: WorkTask, now: number): number {
  const a = t.assigned_at ? Date.parse(t.assigned_at) : NaN, e = t.eta ? Date.parse(t.eta) : NaN
  const est = Number.isFinite(a) && Number.isFinite(e) && e > a && now > 0 ? Math.min(0.99, (now - a) / (e - a)) : 0
  return clamp(Math.max(t.progress, est))
}

/** `now`: factory time (ms); `speed`: the sim speed, so a finished repair stays green for the same wall time at 1×…60×;
 *  `rec`: the machine's recovery after a fast repair (ringRecoveries). */
export function machineRing(id: string, tile: MachineTile | undefined, world: WorldSnapshot | null, now: number, speed = 1, rec: RingRecovery | null = null): MachineRing {
  const items = world?.tasks.items ?? []
  const here = items.filter((t: WorkTask) => t.machine_id === id && (t.status === '執行中' || t.status === '前往中'))
  const ringOf = (task: WorkTask): MachineRing => {
    const p = liveProgress(task, now)
    // a repair on its way reads as the person (the demo's 「派人進廠維修」 step), other tasks keep their kind first
    const going = REPAIR_KINDS.has(task.kind) ? '維修人員前往中' : `${task.kind}（人員前往中）`
    return { kind: 'task', progress: p, color: TASK_COLOR[task.kind] ?? '#5db2f5',
      label: task.status === '前往中' ? going : `${task.kind} ${Math.round(p * 100)}%` }
  }
  const repairing = here.find((t) => REPAIR_KINDS.has(t.kind))
  if (repairing) return ringOf(repairing)
  // the repair is done: 回溫中 (amber, filling over the ramp) while the machine is not back to running, then 已恢復 (green)
  // for DONE_HOLD_WALL_S; over another task of the same plan (設定硫化時間) and instead of the 檢修完成 ring below
  if (rec?.phase === 'recovering' && tile?.status !== 'running') return { kind: 'recover', progress: rec.frac, color: RECOVER_AMBER, label: rec.label }
  if (rec && !ALARM.has(tile?.status ?? '') && (rec.phase === 'recovering' || rec.ageS <= DONE_HOLD_WALL_S)) {
    return { kind: 'recover', progress: 1, color: DONE_GREEN, label: '已恢復' }
  }
  if (here[0]) return ringOf(here[0])
  const hold = Math.max(60_000, DONE_HOLD_WALL_S * 1000 * speed)
  const fixed = rec ? undefined : items.filter((t) => t.machine_id === id && t.status === '完成' && REPAIR_KINDS.has(t.kind) && t.done_at && now - Date.parse(t.done_at) <= hold)
    .sort((a, b) => Date.parse(b.done_at!) - Date.parse(a.done_at!))[0]
  if (fixed) {
    // green only once the fix landed (the lamp left 異常): the gate's R1 檢修 finishes while the card still waits and the
    // runtime lands the fix only after 主管核准, so it must not look 「檢修完成」 over a red machine. Right after 完成 (the
    // fix landing) it stays at 100% in its own colour, then the alarm ring shows again.
    if (!ALARM.has(tile?.status ?? '') || tile?.repaired) return { kind: 'task', progress: 1, color: DONE_GREEN, label: `${fixed.kind}完成` }
    if (now - Date.parse(fixed.done_at!) <= DONE_GRACE_WALL_S * 1000 * speed) {
      return { kind: 'task', progress: 1, color: TASK_COLOR[fixed.kind] ?? '#5db2f5', label: `${fixed.kind} 100%` }
    }
  }
  const m = world?.maintenance.in_progress.find((x) => x.machine_id === id)
  if (m) {
    const s = Date.parse(m.start), e = m.end ? Date.parse(m.end) : NaN
    const p = Number.isFinite(e) && e > s ? clamp((now - s) / (e - s)) : 0.5
    return { kind: 'maint', progress: p, color: '#b39ddb', label: `${m.title} ${Math.round(p * 100)}%` }
  }
  const st = tile?.status
  // #104: 檢修完成 on a machine that stays out of service until the supervisor's 恢復運轉 — green, not an alarm
  if (tile?.repaired) return { kind: 'task', progress: 1, color: DONE_GREEN, label: '檢修完成・待恢復運轉' }
  if (st && ALARM.has(st)) return { kind: 'alarm', progress: 1, color: LIGHT_HEX.red, label: STATUS_ZH[st] ?? st }
  const sp = tile?.step_progress
  if ((st === 'running' || st === 'warning') && sp != null) {
    const press = id.startsWith('CURE-')
    return { kind: 'batch', progress: clamp(sp), color: st === 'warning' ? LIGHT_HEX.amber : LIGHT_HEX.green,
      label: `${press ? '硫化' : '本批'} ${Math.round(sp * 100)}%${st === 'warning' ? `・${STATUS_ZH.warning}` : ''}` }
  }
  if (st === 'running') return { kind: 'idle', progress: 0, color: LIGHT_HEX.green, label: '運轉中' }   // no batch data (mock)
  return { kind: 'idle', progress: 0, color: '#6b7280', label: st === 'offline' ? '離線' : '閒置' }
}
