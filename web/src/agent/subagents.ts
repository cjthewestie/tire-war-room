// Words for the main agent's subagents (warroom/agent/orchestrator.py), shared by 主控台, the Agent page and 狀態機.
import type { SubagentStatus } from '../contracts'
import type { Event, Subagent, WorkTask } from '../contracts'
import { REPAIR_KINDS } from '../aiStatus'
import { machineLabel } from '../factory'

export const SUB_STATUS: Record<SubagentStatus, string> = {
  queued: '排隊中', running: '執行中', waiting_model: '等模型', waiting_approval: '等主管核准', waiting_tasks: '等人員完成',
  done: '完成', failed: '安全中止', interrupted: '已中斷',
}

// ---------------------------------------------------------------- plain words for the supervisor (CJ, 2026-10-20: 「這邊真的很怪」)

const STOP_REASON: [RegExp, string][] = [
  [/核准逾時/, '等核准逾時'], [/主管中斷/, '主管中斷'], [/連續 ?\d+ ?次失敗/, '連續失敗兩次'], [/指揮官要求安全中止|指揮官無回應/, 'Agent Nemo 判斷要停'],
  [/事件處理被取消/, '被取消'], [/內部錯誤/, '系統錯誤'],
]

/** How a closed machine event ended (veteran glossary): 已恢復正常 only when a reading confirmed it (the back-to-normal
 *  time, the outcome 「已修復：…溫度回到 128°C」 or a confirmed hypothesis); a machine taken out and an unverified close say
 *  what really happened. null: not closed, or not a machine fix (a supervisor command, a plant event). */
export function endedText(ev: Event): string | null {
  if (ev.state !== 'closed' || ev.source === 'supervisor' || ev.source === 'plant') return null
  const o = ev.outcome ?? ''
  if (/停用/.test(o)) return '已停用該機，排程已調整'
  if (/未驗證/.test(o)) return '已處置，未用讀值確認'
  if (ev.recovered_at || /^已修復/.test(o) || ev.hypotheses.some((h) => h.status === 'confirmed')) return '已恢復正常'
  return null
}

/** The field work still open on an open mill is slowing or pausing the feed, not a repair (veteran glossary). */
const DOING_NOW: Record<string, string> = { 放慢投料: '放慢投料中', 暫停投料: '暫停投料中' }

/** 規劃中 / 待核准 / 執行中 / 維修中（k/n）/ 放慢投料中（k/n）/ 已恢復正常（現場工作 k/n）/ 完成 / 已停止 — <原因一句話>. With
 *  the event and the world's tasks it tells the repair still running from a machine back to normal while other field
 *  work goes on. */
export function plainStatus(sa: Subagent, ev?: Event | null, tasks: WorkTask[] = []): string {
  const repair = tasks.filter((t) => t.event_id === sa.id && REPAIR_KINDS.has(t.kind))
  const open = repair.filter((t) => t.status !== '完成')
  const doing = open.every((t) => t.kind === open[0]?.kind) ? DOING_NOW[open[0]?.kind ?? ''] : undefined
  const repairing = open.length ? `${doing ?? '維修中'}（${repair.length - open.length}/${repair.length}）` : null
  const ended = ev ? endedText(ev) : null
  switch (sa.status) {
    case 'waiting_approval': return '待核准'
    case 'waiting_tasks':
      if (repairing) return repairing
      if (ended) return `${ended}（現場工作 ${sa.tasks_done}/${sa.tasks_total}）`
      if (ev?.repaired_at && ev.state !== 'closed') return '維修完成，回溫中'
      return `現場工作 ${sa.tasks_done}/${sa.tasks_total}`
    case 'done': return ended ?? '完成'
    case 'interrupted': return '已停止 — 主管中斷'
    case 'failed': {
      const o = sa.outcome ?? ''
      const hit = STOP_REASON.find(([rx]) => rx.test(o))
      const why = hit ? hit[1] : (/安全中止：([^；]+)/.exec(o)?.[1] ?? '').slice(0, 14)
      return `已停止${why ? ` — ${why}` : ''}`
    }
    default: return sa.state === 'execute' || sa.state === 'verify' ? repairing ?? '執行中' : '規劃中'
  }
}

/** 「溫度下降」「溫度過高」: what happened to the machine, without its name or the readings (an alert card's title:
 *  「10 號機溫度下降 128→122.4°C」「異常：B 線開煉機 溫度過高」). */
export function shortWhat(title: string, machineId: string): string {
  const t = title.replace(/^異常[：:]\s*/, '').replace(machineLabel(machineId), '')
  return t.replace(/^\d+ ?號機/, '').replace(/\s*[\d.]+ ?→ ?[\d.]+\S*$/, '').trim()
}
export const anomalyWhat = (ev: Event) => shortWhat(ev.alert_card?.title ?? '', ev.machine_id)

/** 「#M-201 可能晚交」「10 號硫化機 溫度下降」: what this subagent is about, whole (never cut inside a number: the list
 *  ends a long one with an ellipsis). */
export function whatOf(sa: Subagent, ev?: Event | null): string {
  if (sa.source === 'supervisor') return (ev?.command ?? sa.title).replace(/，?請.*$/, '')
  if (sa.source === 'plant') return ev?.alert_card?.title ?? sa.title
  const what = shortWhat(ev?.alert_card?.title ?? sa.title, sa.machine_id)
  return what ? `${machineLabel(sa.machine_id)} ${what}` : machineLabel(sa.machine_id)
}

/** 「128°C」 out of the outcome 「已修復：3 號機 溫度回到 128°C（維修完成 10:10、恢復 10:11）」. */
export const backTo = (outcome: string | null) => /回到 ?([\d.]+ ?(?:°C|kgf\/cm²|L\/min)?)/.exec(outcome ?? '')?.[1] ?? null

/** 「S16（加硫組）」 → 「S16」: the runtime adds the dataset's group name to a person; screens name the task's own team. */
export const workerName = (who: string | null | undefined) => (who ?? '').replace(/（[^）]*）$/, '')

export const isOpen = (sa: Subagent) => !['done', 'failed', 'interrupted'].includes(sa.status)
