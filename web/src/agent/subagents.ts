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

/** 規劃中 / 等你核准 / 執行中 / 等現場完成（k/n）/ 完成 / 已停止 — <原因一句話> */
export function plainStatus(sa: Subagent): string {
  switch (sa.status) {
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
