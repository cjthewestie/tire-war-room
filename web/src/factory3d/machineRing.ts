// What the progress ring above one machine shows (issue #36): pure, shared by MachineRings, the focus tag and the e2e hook.
// Priority: a worker task there (暖機 / 檢修 / 補料 ..., issue #17), maintenance in progress, alarm / stopped / out of
// service, the running batch or cure (MachineTile.step_progress), else idle.
// Fast repair (Kyle 10-21): a repair task (檢修／停用檢修／放慢投料／暫停投料) on the machine wins over other tasks, its ring
// follows the clock between world steps (a 3-second repair would otherwise jump 0 → done), and a just-finished repair
// shows a full green ring for a few seconds.
import type { MachineTile, WorkTask, WorldSnapshot } from '../contracts'
import { REPAIR_KINDS } from '../aiStatus'

export type RingKind = 'task' | 'maint' | 'alarm' | 'batch' | 'idle'
export interface MachineRing { kind: RingKind; progress: number; color: string; label: string }

const TASK_COLOR: Record<string, string> = {
  暖機: '#ff8a50', 檢修: '#b39ddb', 停用檢修: '#b39ddb', 品管: '#b39ddb',
  補料: '#f5b83d', 調料: '#f5b83d', 換模準備: '#f5b83d', 盤點: '#f5b83d',
}
const ALARM = new Set(['alarm', 'stopped', 'out_of_service'])
const STATUS_ZH: Record<string, string> = { alarm: '異常', stopped: '停機', out_of_service: '停用', warning: '注意' }
const DONE_GREEN = '#3fcf5a'      // the status lamp's green (layout.ts LIGHT_HEX)
const DONE_HOLD_WALL_S = 6        // a finished repair stays green this long (wall seconds; at least one factory minute)
const clamp = (x: number) => Math.max(0, Math.min(1, x))

/** The task's progress now: the server's (as of the last world step), or where the clock is between assignment and its
 *  ETA, kept just under done until the server says 完成. */
function liveProgress(t: WorkTask, now: number): number {
  const a = t.assigned_at ? Date.parse(t.assigned_at) : NaN, e = t.eta ? Date.parse(t.eta) : NaN
  const est = Number.isFinite(a) && Number.isFinite(e) && e > a && now > 0 ? Math.min(0.99, (now - a) / (e - a)) : 0
  return clamp(Math.max(t.progress, est))
}

/** `now`: factory time (ms); `speed`: the sim speed, so a finished repair stays green for the same wall time at 1×…60×. */
export function machineRing(id: string, tile: MachineTile | undefined, world: WorldSnapshot | null, now: number, speed = 1): MachineRing {
  const items = world?.tasks.items ?? []
  const here = items.filter((t: WorkTask) => t.machine_id === id && (t.status === '執行中' || t.status === '前往中'))
  const task = here.find((t) => REPAIR_KINDS.has(t.kind)) ?? here[0]
  if (task) {
    const p = liveProgress(task, now)
    return { kind: 'task', progress: p, color: TASK_COLOR[task.kind] ?? '#5db2f5',
      label: `${task.kind}${task.status === '前往中' ? '（人員前往中）' : ` ${Math.round(p * 100)}%`}` }
  }
  const hold = Math.max(60_000, DONE_HOLD_WALL_S * 1000 * speed)
  const fixed = items.find((t) => t.machine_id === id && t.status === '完成' && REPAIR_KINDS.has(t.kind) && t.done_at && now - Date.parse(t.done_at) <= hold)
  if (fixed) return { kind: 'task', progress: 1, color: DONE_GREEN, label: `${fixed.kind}完成` }
  const m = world?.maintenance.in_progress.find((x) => x.machine_id === id)
  if (m) {
    const s = Date.parse(m.start), e = m.end ? Date.parse(m.end) : NaN
    const p = Number.isFinite(e) && e > s ? clamp((now - s) / (e - s)) : 0.5
    return { kind: 'maint', progress: p, color: '#b39ddb', label: `${m.title} ${Math.round(p * 100)}%` }
  }
  const st = tile?.status
  if (st && ALARM.has(st)) return { kind: 'alarm', progress: 1, color: '#ff5d52', label: STATUS_ZH[st] ?? st }
  const sp = tile?.step_progress
  if ((st === 'running' || st === 'warning') && sp != null) {
    const press = id.startsWith('CURE-')
    return { kind: 'batch', progress: clamp(sp), color: st === 'warning' ? '#f5b83d' : '#76b900',
      label: `${press ? '硫化' : '本批'} ${Math.round(sp * 100)}%${st === 'warning' ? `・${STATUS_ZH.warning}` : ''}` }
  }
  if (st === 'running') return { kind: 'idle', progress: 0, color: '#76b900', label: '運轉中' }   // no batch data (mock)
  return { kind: 'idle', progress: 0, color: '#6b7280', label: st === 'offline' ? '離線' : '閒置' }
}
