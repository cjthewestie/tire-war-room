// 今天處理過的任務 on the Agent page (CJ, 2026-10-22: 「今天處理過的任務都要記錄在agent 頁面不可以清掉」): read-only, from
// GET /api/tasklog (the audit file), so 重來, a scenario load, a server restart or the day-loop wrap never empty it.
// No delete or edit anywhere. A row opens its event when it is still in this scenario; the report link always works.
import { useEffect, useState } from 'react'
import { apiGet } from '../transport'
import { useWarRoom } from '../useWarRoom'
import { fmtHM } from '../format'
import { machineLabel } from '../factory'
import { plain } from '../plain'

interface Row {
  id: string; opened_at: string; closed_at: string | null; machine_id: string | null; order_id: string | null
  title: string | null; final: string; outcome: string | null; scenario: string | null; loop: number | null; report: string
  approvals: { decision: string; by: string; version: number }[]
}
interface Day { date: string; count: number; groups: { loop: number | null; scenario: string | null; rows: Row[] }[] }

export function TaskLog({ onPick }: { onPick?: (eventId: string) => void }) {
  const { state, transport } = useWarRoom()
  const [days, setDays] = useState<Day[]>([])
  const live = transport?.mode === 'live'
  useEffect(() => {
    if (!live) return
    let off = false
    const pull = () => apiGet<{ days: Day[] }>('/api/tasklog').then((d) => { if (!off) setDays(d.days) }).catch(() => {})
    pull()
    const t = setInterval(pull, 8000)
    return () => { off = true; clearInterval(t) }
  }, [live])
  const known = new Set((state.snapshot?.events ?? []).map((e) => e.id))
  if (!live) return null
  const today = days[0]
  return (
    <section className="tasklog" aria-label="今天處理過的任務" data-testid="tasklog">
      <h3 className="sub">今天處理過的任務{today ? <small className="muted">　{today.date}・{today.count} 件</small> : null}</h3>
      {!today && <p className="muted small">今天還沒有處理過的任務。</p>}
      {today?.groups.map((g, gi) => (
        <div key={gi} className="tl-group">
          {(g.loop != null || g.scenario) && <p className="muted small tl-tag">{g.loop != null ? `循環 ${g.loop}` : ''}{g.loop != null && g.scenario ? '・' : ''}{g.scenario ?? ''}</p>}
          <ul className="tl-list">
            {[...g.rows].reverse().map((r) => (
              <li key={`${r.id}-${r.opened_at}`} className={`tl-row f-${r.final}`} data-testid="tasklog-row" data-event={r.id}>
                <button type="button" disabled={!known.has(r.id) || !onPick} onClick={() => onPick?.(r.id)} title={known.has(r.id) ? '打開這件的完整過程' : '這件屬於之前載入的情境'}>
                  <span className="tl-t">{fmtHM(r.opened_at)}{r.closed_at ? `–${fmtHM(r.closed_at)}` : ''}</span>
                  <b>{r.order_id ?? (r.machine_id ? machineLabel(r.machine_id) : '任務')}</b>
                  <span className="tl-what">{plain(r.title ?? r.id).slice(0, 28)}</span>
                  <span className="tl-final">{r.final}</span>
                  {r.approvals.length > 0 && <small className="muted">{r.approvals.map((a) => `v${a.version} ${a.decision}（${a.by.replace(/@.*/, '')}）`).join('、')}</small>}
                  {r.outcome && <small className="muted tl-out">{plain(r.outcome).slice(0, 60)}</small>}
                </button>
                <a className="tl-report" href={r.report} target="_blank" rel="noreferrer">處理報告</a>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </section>
  )
}
