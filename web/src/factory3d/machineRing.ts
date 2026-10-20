// What the progress ring above one machine shows (issue #36): pure, shared by MachineRings, the focus tag and the e2e hook.
// Priority: a worker task there (暖機 / 檢修 / 補料 ..., issue #17), maintenance in progress, alarm / stopped / out of
// service, the running batch or cure (MachineTile.step_progress), else idle.
import type { MachineTile, WorkTask, WorldSnapshot } from '../contracts'

export type RingKind = 'task' | 'maint' | 'alarm' | 'batch' | 'idle'
export interface MachineRing { kind: RingKind; progress: number; color: string; label: string }

const TASK_COLOR: Record<string, string> = {
  暖機: '#ff8a50', 檢修: '#b39ddb', 停用檢修: '#b39ddb', 品管: '#b39ddb',
  補料: '#f5b83d', 調料: '#f5b83d', 換模準備: '#f5b83d', 盤點: '#f5b83d',
}
const ALARM = new Set(['alarm', 'stopped', 'out_of_service'])
const STATUS_ZH: Record<string, string> = { alarm: '異常', stopped: '停機', out_of_service: '停用', warning: '注意' }
const clamp = (x: number) => Math.max(0, Math.min(1, x))

export function machineRing(id: string, tile: MachineTile | undefined, world: WorldSnapshot | null, now: number): MachineRing {
  const task = world?.tasks.items.find((t: WorkTask) => t.machine_id === id && (t.status === '執行中' || t.status === '前往中'))
  if (task) {
    return { kind: 'task', progress: clamp(task.progress), color: TASK_COLOR[task.kind] ?? '#5db2f5',
      label: `${task.kind}${task.status === '前往中' ? '（人員前往中）' : ` ${Math.round(task.progress * 100)}%`}` }
  }
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
