// 快速維修的修復預估 (Kyle 10-21 demo flow: 主管核准 → NemoClaw 派人進廠維修 → 維修完成 → 回溫 → 恢復正常). With
// REPAIR_FAST_S > 0 a repair task runs repair_fast_s WALL seconds at any sim speed, then the readings ramp back over
// recover_fast_s wall seconds (RECOVER_FAST_S). The phone's 修復預估 says those wall seconds (「約 16 秒」), not the
// verification window (「約 30 分鐘」) or a factory-clock time.
// A task's remaining seconds come from its progress (wall-based on the server, work.py _view), never from its factory
// eta, which was converted at the speed the task was made with: a 10× ↔ 60× switch mid-repair would move that one.
// Pure (no React, no fetch): RepairEta renders it, e2e/eta-unit.mjs checks it.
import type { Event, HealthStatus, WorkTask } from '../contracts'
import { REPAIR_KINDS } from '../aiStatus'

export interface FastRepair { repair_s: number; recover_s: number }
type TaskLike = Pick<WorkTask, 'status' | 'progress'>

/** notify_teams kinds whose task repairs the machine (work.py plan_tasks → 檢修／停用檢修／放慢投料／暫停投料). */
const REPAIR_NOTIFY = new Set(['maintenance', 'machine_out', 'slow_feed', 'pause_feed'])

/** The fast-repair seconds from GET /api/health; null when the server runs the real durations (repair_fast_s 0) or is
 *  older (no field). A missing recover_fast_s counts as 0. */
export function fastRepairOf(h: (Pick<HealthStatus, 'repair_fast_s'> & { recover_fast_s?: number }) | null | undefined): FastRepair | null {
  const repair = Number(h?.repair_fast_s ?? 0)
  if (!Number.isFinite(repair) || repair <= 0) return null
  const recover = Number(h?.recover_fast_s ?? 0)
  return { repair_s: repair, recover_s: Number.isFinite(recover) && recover > 0 ? recover : 0 }
}

const num = (s: number) => String(Math.round(s * 10) / 10)
/** 「約 N 秒」, rounded up and never below 1 while something is still left. */
export const secText = (s: number) => `約 ${Math.max(1, Math.ceil(s - 1e-6))} 秒`

/** 多久修好: 「約 16 秒（維修 12 秒＋回溫 4 秒）」 (no 回溫 part when the server has none). */
export function fastWindowText(f: FastRepair): string {
  return `約 ${num(f.repair_s + f.recover_s)} 秒（維修 ${num(f.repair_s)} 秒${f.recover_s > 0 ? `＋回溫 ${num(f.recover_s)} 秒` : ''}）`
}

export const isRepairTask = (t: Pick<WorkTask, 'kind'>) => REPAIR_KINDS.has(t.kind)

/** Is this event fixed by the fast flow? A machine event whose crew repairs the machine: a repair task exists already, or
 *  the plan still holds the repair notice the card will release. Plant events and supervisor commands are not. */
export function fastApplies(ev: Pick<Event, 'source' | 'plan' | 'steps'>, tasks: Pick<WorkTask, 'kind'>[]): boolean {
  if (ev.source === 'plant' || ev.source === 'supervisor') return false
  if (tasks.some(isRepairTask)) return true
  const steps = [...(ev.plan?.steps ?? []), ...(ev.steps ?? []).map((s) => s.step)]
  return steps.some((s) => s?.tool === 'notify_teams' && REPAIR_NOTIFY.has(String(s.args?.kind)))
}

/** Wall seconds this fast repair task still needs: (1 − progress) × repair_fast_s. */
export function taskLeftS(t: TaskLike, f: FastRepair): number {
  if (t.status === '完成') return 0
  const p = Math.min(1, Math.max(0, Number(t.progress) || 0))
  return (1 - p) * f.repair_s
}

/** 預計完成, in wall seconds from now. `lead` goes before the bold `text`:
 *  - nobody on it yet: the whole formula, 「現在核准的話，約 16 秒後」 before the decision, 「約 16 秒後」 after it
 *  - the crew repairing: the slowest open repair task's remaining seconds + 回溫, 「約 9 秒後」
 *  - every repair done (or Event.repaired_at): 「維修已完成，回溫中（約 4 秒）」 */
export function fastDoneText(repair: TaskLike[], f: FastRepair, decided: boolean, repaired = false): { lead: string; text: string } {
  const open = repair.filter((t) => t.status !== '完成')
  const started = repair.some((t) => t.status !== '待派')
  if (repaired || (started && !open.length)) {
    return { lead: '', text: f.recover_s > 0 ? `維修已完成，回溫中（${secText(f.recover_s)}）` : '維修已完成，確認恢復中' }
  }
  if (started) return { lead: '', text: `${secText(Math.max(...open.map((t) => taskLeftS(t, f))) + f.recover_s)}後` }
  return { lead: decided ? '' : '現在核准的話，', text: `約 ${num(f.repair_s + f.recover_s)} 秒後` }
}
