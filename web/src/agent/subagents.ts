// Words for the main agent's subagents (warroom/agent/orchestrator.py), shared by 主控台, the Agent page and 狀態機.
import type { SubagentStatus } from '../contracts'
import type { Event, Subagent } from '../contracts'
import { machineLabel } from '../factory'

export const SUB_STATUS: Record<SubagentStatus, string> = {
  queued: '排隊中', running: '執行中', waiting_model: '等模型', waiting_approval: '等主管核准', waiting_tasks: '等人員完成',
  done: '完成', failed: '安全中止', interrupted: '已中斷',
}
export const SUB_TONE: Record<SubagentStatus, 'live' | 'wait' | 'done' | 'bad'> = {
  queued: 'wait', running: 'live', waiting_model: 'wait', waiting_approval: 'wait', waiting_tasks: 'live', done: 'done', failed: 'bad', interrupted: 'bad',
}

// ---------------------------------------------------------------- plain words for the supervisor (CJ, 2026-10-20: 「這邊真的很怪」)

const STOP_REASON: [RegExp, string][] = [
  [/核准逾時/, '等核准逾時'], [/主管中斷/, '主管中斷'], [/連續 ?\d+ ?次失敗/, '連續失敗兩次'], [/指揮官要求安全中止|指揮官無回應/, 'Agent Nemo 判斷要停'],
  [/事件處理被取消/, '被取消'], [/內部錯誤/, '系統錯誤'],
]

/** 規劃中 / 等你核准 / 執行中 / 等現場完成（k/n）/ 完成 / 已停止 — <原因一句話>. With `ev` and its worker tasks (the World's,
 *  as 主控台 counts them), an ended event reads as 主控台 shows it, whatever the subagent status says: a fast repair
 *  closed the event while the subagent still said 「等現場完成（1/5）」 under 處理中 and 主控台 said 已結案 (#92 review). */
export function plainStatus(sa: Subagent, ev?: Event | null, tasks?: { status: string }[]): string {
  if (ev && ended(ev) && sa.status !== 'interrupted') {
    const left = (tasks ?? []).filter((t) => t.status !== '完成').length
    if (ev.state === 'closed' && left) return `等現場完成（${tasks!.length - left}/${tasks!.length}）`
    if (ev.state === 'closed') return '完成'
  }
  const status = ev && ended(ev) && isOpen(sa) ? 'failed' : sa.status
  switch (status) {
    case 'waiting_approval': return '等你核准'
    case 'waiting_tasks': return `等現場完成（${sa.tasks_done}/${sa.tasks_total}）`
    case 'done': return '完成'
    case 'interrupted': return '已停止 — 主管中斷'
    case 'failed': {
      const o = sa.outcome ?? ''
      const hit = STOP_REASON.find(([rx]) => rx.test(o))
      const why = hit ? hit[1] : (/安全中止：([^；]+)/.exec(o)?.[1] ?? '').slice(0, 14)
      return `已停止${why ? ` — ${why}` : ''}`
    }
    default: return sa.state === 'execute' || sa.state === 'verify' ? '執行中' : '規劃中'
  }
}

/** 「#M-201 可能晚交」「10 號硫化機溫度下降」: what this subagent is about, short. */
export function whatOf(sa: Subagent, ev?: Event | null): string {
  if (sa.source === 'supervisor') return (ev?.command ?? sa.title).replace(/，?請.*$/, '').slice(0, 18)
  if (sa.source === 'plant') return (ev?.alert_card?.title ?? sa.title).slice(0, 18)
  const title = (ev?.alert_card?.title ?? sa.title).replace(/^\d+ ?號機/, '')
  return `${machineLabel(sa.machine_id)}${title}`.slice(0, 20)
}

export const isOpen = (sa: Subagent) => !['done', 'failed', 'interrupted'].includes(sa.status)
/** Ended by the event's own state (closed / safe_stop), as 主控台 and the overview's task list count it. */
export const ended = (ev: Event) => ev.state === 'closed' || ev.state === 'safe_stop'
/** Still 處理中 on the Agent page exactly when 主控台 lists it as open: not ended, or closed with its people still at work
 *  (主控台 ④ 執行中，等確認完成, web/src/agent/homeItems.ts). `tasks`: the World's worker tasks of this event. */
export const eventOpen = (ev: Event, tasks: { status: string }[]) => !ended(ev) || (ev.state === 'closed' && tasks.some((t) => t.status !== '完成'))
