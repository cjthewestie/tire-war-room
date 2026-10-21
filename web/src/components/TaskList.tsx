// Worker tasks (issue #17): what people do after an approval. Code turns each approved step into tasks
// (warroom/sim/work.py); status 待派 → 前往中 → 執行中 → 完成 comes live from the world (GET /api/world, WS "world").
// 人員回報 mode (CJ 2026-10-22: 「加回報」): the worker reports 開始 / 完成 on /worker; here the supervisor sees who
// reported (with photo), a gentle 逾時提醒, and can 代為完成 with a required reason (POST /api/tasks/{id}/override).
import { useState } from 'react'
import type { TaskStatus, WorkTask } from '../contracts'
import { fmtHM } from '../format'
import { adminFetch } from '../transport'
import { ProgressRing, type RingTone } from './ProgressRing'
import './work.css'

const TONE: Record<TaskStatus, RingTone> = { 待派: 'idle', 前往中: 'warn', 執行中: 'live', 完成: 'done' }

export function TaskList({ tasks, eventId, limit, compact, testId = 'task-list' }: {
  tasks: WorkTask[]; eventId?: string; limit?: number; compact?: boolean; testId?: string
}) {
  const rows = (eventId ? tasks.filter((t) => t.event_id === eventId) : tasks).slice(0, limit ?? 50)
  if (!rows.length) return <p className="muted small" data-testid={testId}>{eventId ? '核准後，人員任務會出現在這裡' : '目前沒有人員任務'}</p>
  return (
    <ul className={`tasks ${compact ? 'compact' : ''}`} data-testid={testId}>
      {rows.map((t) => (
        <li key={t.id} className={`task st-${t.status}`} data-testid="task-row" data-status={t.status} data-task={t.id}>
          <ProgressRing value={t.progress} tone={TONE[t.status]} size={compact ? 22 : 28} title={`${t.status} ${Math.round(t.progress * 100)}%`} />
          <div className="task-b">
            <div className="w-row">
              <span><i className={`tchip st-${t.status}`}>{t.status}</i> <b>{t.kind}</b> {t.title}</span>
              <span className="muted small">{t.status === '完成' ? `${fmtHM(t.done_at)} 完成` : t.eta ? `預計 ${fmtHM(t.eta)}` : ''}</span>
            </div>
            <div className="muted small">
              {t.worker_name ? `${t.worker_name}・${t.team_label}` : `待派：${t.team_label}`}・{t.where}
              {!compact && t.after && t.status === '待派' ? `・等 ${t.after} 完成` : ''}
            </div>
            {t.mode === 'report' && <ReportLine t={t} compact={!!compact} />}
          </div>
        </li>
      ))}
    </ul>
  )
}

function ReportLine({ t, compact }: { t: WorkTask; compact: boolean }) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const who = (t.worker_name ?? '').split('（')[0]
  if (t.status === '完成') {
    return (
      <div className="task-rep small" data-testid="task-reported">
        {t.override_reason
          ? <>主管 {fmtHM(t.done_at)} 代為完成（原因：{t.override_reason}）</>
          : <>{who} {fmtHM(t.done_at)} 回報完成{t.photo_sha256 && <>（<a href={`/api/tasks/${t.id}/photo`} target="_blank" rel="noreferrer">附照片</a>）</>}</>}
        {t.report_note && !t.override_reason && <span className="muted">・{t.report_note}</span>}
      </div>
    )
  }
  const send = async () => {
    if (!reason.trim()) { setErr('請填原因'); return }
    setBusy(true); setErr(null)
    try {
      await adminFetch(`/api/tasks/${t.id}/override`, { method: 'POST', body: { reason: reason.trim() } })
      setOpen(false); setReason('')
    } catch (e) {
      setErr(String(e).replace(/^Error: /, ''))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="task-rep small">
      {t.overdue && <span className="task-overdue" role="status" data-testid="task-overdue">逾時提醒：已超過預計 {t.work_min * 2} 分鐘，{who || '負責人'}尚未回報完成</span>}
      {!t.overdue && <span className="muted">{t.status === '待派' ? '核准後派人' : t.status === '前往中' ? `等 ${who} 回報到達` : `等 ${who} 回報完成`}</span>}
      {!compact && !open && <button type="button" className="btn-ghost task-ovr" onClick={() => setOpen(true)} data-testid="task-override">代為完成</button>}
      {!compact && open && (
        <span className="task-ovr-f">
          <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="原因（必填，例：手機沒電，口頭回報）" maxLength={300} aria-label="代為完成原因" data-testid="task-override-reason" />
          <button type="button" className="btn-ghost" disabled={busy} onClick={send}>確認</button>
          <button type="button" className="btn-ghost" onClick={() => { setOpen(false); setErr(null) }}>取消</button>
        </span>
      )}
      {err && <span className="task-err" role="alert">{err}</span>}
    </div>
  )
}
