// Issue #59: /report, the incident report for one event (?event=<id>). Built by code (build.ts) from the records;
// the supervisor adds the prevention measures, then copies the text or prints / saves it as PDF.
import { useEffect, useMemo, useState } from 'react'
import { getJSON } from '../api'
import { API, apiPath, type ApprovalRequest, type AuditEntry, type Event } from '../contracts'
import { machineLabel } from '../factory'
import { fmtHM, fmtMD } from '../format'
import { useWarRoom } from '../useWarRoom'
import { AiReport } from '../agent/AiReport'
import { buildReport, reportText } from './build'

const POLL_MS = 5000
const noteKey = (id: string) => `report-prevention:${id}`
function readNote(id: string): string {
  try { return localStorage.getItem(noteKey(id)) ?? '' } catch { return '' }
}

export function Report() {
  const { state } = useWarRoom()
  const [events, setEvents] = useState<Event[]>([])
  const [picked, setPicked] = useState<string | null>(() => new URLSearchParams(location.search).get('event'))
  const [ev, setEv] = useState<Event | null>(null)
  const [approvals, setApprovals] = useState<ApprovalRequest[]>([])
  const [chainOk, setChainOk] = useState<boolean | null>(null)
  const [auditCount, setAuditCount] = useState<number | null>(null)
  const [notes, setNotes] = useState<Record<string, string>>({})
  const [copied, setCopied] = useState<'ok' | 'manual' | null>(null)

  useEffect(() => {
    let live = true
    const pull = () => {
      getJSON<Event[]>(API.events).then((v) => live && setEvents(v)).catch(() => {})
      getJSON<ApprovalRequest[]>(API.approvals).then((v) => live && setApprovals(v)).catch(() => {})
      getJSON<{ audit_chain_ok?: boolean }>('/api/health/components').then((v) => live && setChainOk(v.audit_chain_ok ?? null)).catch(() => {})
    }
    pull()
    const id = setInterval(pull, POLL_MS)
    return () => { live = false; clearInterval(id) }
  }, [])

  // default: the newest event
  const sorted = useMemo(() => [...events].sort((a, b) => b.opened_at.localeCompare(a.opened_at)), [events])
  const current = picked ?? sorted[0]?.id ?? null
  useEffect(() => {
    if (!current) return
    let live = true
    const pull = () => {
      getJSON<Event>(apiPath(API.event, { event_id: current })).then((v) => live && setEv(v)).catch(() => live && setEv(null))
      getJSON<AuditEntry[]>(`${API.audit}?event_id=${encodeURIComponent(current)}&limit=5000`).then((v) => live && setAuditCount(v.length)).catch(() => {})
    }
    pull()
    const id = setInterval(pull, POLL_MS)
    return () => { live = false; clearInterval(id) }
  }, [current])

  const choose = (id: string) => {
    setPicked(id)
    const u = new URL(location.href); u.searchParams.set('event', id); history.replaceState(null, '', u)
  }
  const prevention = current ? (notes[current] ?? readNote(current)) : ''
  const savePrevention = (v: string) => {
    if (!current) return
    setNotes((n) => ({ ...n, [current]: v }))
    try { localStorage.setItem(noteKey(current), v) } catch { /* private mode: kept for this visit */ }
  }

  const taskView = state.world?.tasks
  const report = useMemo(() => (ev ? buildReport({ ev, approvals, tasks: taskView?.items ?? [], chainOk, auditCount }) : null),
    [ev, approvals, taskView, chainOk, auditCount])
  const text = report ? reportText(report, prevention) : ''
  const copy = () => {
    const manual = () => setCopied('manual')
    try { navigator.clipboard.writeText(text).then(() => setCopied('ok'), manual) } catch { manual() }
    setTimeout(() => setCopied(null), 4000)
  }

  return (
    <div className="rp">
      <header className="rp-top no-print">
        <div className="rp-row">
          <h1>異常處理報告</h1>
          <button type="button" onClick={copy} disabled={!report} data-testid="rp-copy">複製報告文字</button>
          <button type="button" className="primary" onClick={() => window.print()} disabled={!report}>列印／存 PDF</button>
        </div>
        <label className="rp-pick">
          <span>事件</span>
          <select value={current ?? ''} onChange={(e) => choose(e.target.value)} data-testid="rp-pick">
            {sorted.length === 0 && <option value="">目前沒有事件</option>}
            {sorted.map((e) => (
              <option key={e.id} value={e.id}>{fmtMD(e.opened_at)} {fmtHM(e.opened_at)}・{machineLabel(e.machine_id)}・{e.alert_card?.title ?? e.anomaly.message}</option>
            ))}
          </select>
        </label>
        {copied === 'ok' && <p className="rp-note" role="status">已複製，可以貼到郵件或群組。</p>}
        {copied === 'manual' && <p className="rp-note" role="status">這個瀏覽器不讓網頁複製，請選取下方文字手動複製。</p>}
      </header>

      {!report && <p className="rp-empty">{sorted.length ? '讀取事件中…' : '目前沒有事件。跑一個情境（例如 7 號硫化機溫降）結案後再回來。'}</p>}
      {report && (
        <article className="rp-doc" data-testid="rp-doc">
          <h2 className="rp-title">{report.title}</h2>
          {ev && <AiReport ev={ev} />}
          <dl className="rp-head">
            {report.head.map((h) => <div key={h.k} className={h.tone ? `t-${h.tone}` : ''}><dt>{h.k}</dt><dd>{h.text}</dd></div>)}
          </dl>
          {report.parts.map((p) => (
            <section key={p.id} className="rp-part" data-testid={`rp-${p.id}`}>
              {p.id === 'audit' && (
                <div className="rp-part rp-prevent">
                  <h3>七、預防措施</h3>
                  <textarea className="no-print" value={prevention} onChange={(e) => savePrevention(e.target.value)}
                    placeholder="由主管填寫：例如 H2 蒸汽管線每週點檢、7 號機溫度計校正、夜班加開巡檢一次" data-testid="rp-prevent" />
                  <p className="print-only">{prevention.trim() || '（未填）'}</p>
                </div>
              )}
              <h3>{p.title}</h3>
              {p.rows.length === 0 ? <p className="rp-none">{p.empty}</p> : (
                <ul>
                  {p.rows.map((r, i) => (
                    <li key={i} className={r.tone ? `t-${r.tone}` : ''}>
                      {r.time && <time>{r.time}</time>}
                      <span>{r.k && <b>{r.k}</b>}{r.text}</span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          ))}
        </article>
      )}
      {report && (
        <details className="rp-raw no-print" open={copied === 'manual'}>
          <summary>報告文字（純文字）</summary>
          <textarea readOnly value={text} data-testid="rp-text" />
        </details>
      )}
    </div>
  )
}
