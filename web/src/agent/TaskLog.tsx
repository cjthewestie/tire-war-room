// 今天處理過的任務 on the Agent page (CJ, 2026-10-22: 「今天處理過的任務都要記錄在agent 頁面不可以清掉」): read-only, from
// GET /api/tasklog (the audit file), so 重來, a scenario load, a server restart or the day-loop wrap never empty it.
// No delete or edit anywhere. A row opens its event when it is still in this scenario; the report link always works.
// The data (useTaskLog, tasklogData.ts) is shared with 主控台's 今天處理過的事 (HomeOverview).
import { useWarRoom } from '../useWarRoom'
import { fmtHM } from '../format'
import { machineLabel } from '../factory'
import { plain } from '../plain'
import { decisionsText, outcomeText, scenarioLabel, useTaskLog } from './tasklogData'

export function TaskLog({ onPick }: { onPick?: (eventId: string) => void }) {
  const { state } = useWarRoom()
  const days = useTaskLog()
  const known = new Set((state.snapshot?.events ?? []).map((e) => e.id))
  if (!days) return null
  const today = days[0]
  return (
    <section className="tasklog" aria-label="今天處理過的任務" data-testid="tasklog">
      <h3 className="sub">今天處理過的任務{today ? <small className="muted">　{today.date}・{today.count} 件</small> : null}</h3>
      {!today && <p className="muted small">今天還沒有處理過的任務。</p>}
      {today?.groups.map((g, gi) => (
        <div key={gi} className="tl-group">
          {(g.loop != null || g.scenario) && <p className="muted small tl-tag">{g.loop != null ? `循環 ${g.loop}` : ''}{g.loop != null && g.scenario ? '・' : ''}{scenarioLabel(g.scenario)}</p>}
          <ul className="tl-list">
            {[...g.rows].reverse().map((r) => (
              <li key={`${r.id}-${r.opened_at}`} className={`tl-row f-${r.final}`} data-testid="tasklog-row" data-event={r.id}>
                <button type="button" disabled={!known.has(r.id) || !onPick} onClick={() => onPick?.(r.id)} title={known.has(r.id) ? '打開這件的完整過程' : '這件屬於之前載入的情境'}>
                  <span className="tl-t">{fmtHM(r.opened_at)}{r.closed_at ? `–${fmtHM(r.closed_at)}` : ''}</span>
                  <b>{r.order_id ?? (r.machine_id ? machineLabel(r.machine_id) : '任務')}</b>
                  <span className="tl-what">{plain(r.title ?? r.id)}</span>
                  <span className="tl-final">{r.final}</span>
                  {r.approvals.length > 0 && <small className="muted">{decisionsText(r)}</small>}
                  {r.outcome && <small className="muted tl-out">{outcomeText(r)}</small>}
                </button>
                <a className="tl-report" href={r.report} target="_blank" rel="noreferrer">異常報告</a>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </section>
  )
}
