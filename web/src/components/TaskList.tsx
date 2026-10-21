// Worker tasks (issue #17): what people do after an approval. Code turns each approved step into tasks
// (warroom/sim/work.py); status 待派 → 前往中 → 執行中 → 完成 comes live from the world (GET /api/world, WS "world").
import type { TaskStatus, WorkTask } from '../contracts'
import { TEAM_LABELS, machineLabel } from '../factory'
import { fmtHM } from '../format'
import { workerName } from '../agent/subagents'
import { ProgressRing, type RingTone } from './ProgressRing'
import './work.css'

const TONE: Record<TaskStatus, RingTone> = { 待派: 'idle', 前往中: 'warn', 執行中: 'live', 完成: 'done' }

export function TaskList({ tasks, eventId, limit, compact, testId = 'task-list' }: {
  tasks: WorkTask[]; eventId?: string; limit?: number; compact?: boolean; testId?: string
}) {
  const rows = (eventId ? tasks.filter((t) => t.event_id === eventId) : tasks).slice(0, limit ?? 50)
  if (!rows.length) return <p className="muted small" data-testid={testId}>{eventId ? '核准後，現場工作會出現在這裡' : '目前沒有現場工作'}</p>
  return (
    <ul className={`tasks ${compact ? 'compact' : ''}`} data-testid={testId}>
      {rows.map((t) => {
        // 「S16・硫化機組・1 號硫化機」: the machine by its name, the team as every screen says it (TEAM_LABELS)
        const team = TEAM_LABELS[t.team] ?? t.team_label, where = t.machine_id ? machineLabel(t.machine_id) : t.where
        return (
          <li key={t.id} className={`task st-${t.status}`} data-testid="task-row" data-status={t.status} data-task={t.id}>
            <ProgressRing value={t.progress} tone={TONE[t.status]} size={compact ? 22 : 28} title={`${t.status} ${Math.round(t.progress * 100)}%`} />
            <div className="task-b">
              <div className="w-row">
                <span><i className={`task-st st-${t.status}`}>{t.status}</i> <b>{t.kind}</b> {t.title}</span>
                <span className="muted small">{t.status === '完成' ? `${fmtHM(t.done_at)} 完成` : t.eta ? `預計 ${fmtHM(t.eta)}` : ''}</span>
              </div>
              <div className="muted small">
                {t.worker_name ? `${workerName(t.worker_name)}・${team}` : `待派：${team}`}・{where}
                {!compact && t.after && t.status === '待派' ? `・等 ${t.after} 完成` : ''}
              </div>
            </div>
          </li>
        )
      })}
    </ul>
  )
}
