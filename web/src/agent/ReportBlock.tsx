// 處理報告 at the end of the Agent page's 處理歷程 (fast repair, Kyle 10-21): once the event closes, the incident report of
// /report (report/build.ts: built by code from the records, no model) appears under its transcript, with
// 「開啟完整報告」 to /report?event=<id>. Each of 3 concurrent events gets its own when it closes.
import { useEffect, useMemo, useState } from 'react'
import { API, type ApprovalRequest, type Event } from '../contracts'
import { apiGet } from '../transport'
import { useWarRoom } from '../useWarRoom'
import { buildReport, reportText } from '../report/build'
import { ToolIcon } from './ToolIcon'
import { AiReport } from './AiReport'

export function ReportBlock({ ev }: { ev: Event }) {
  const { state, transport } = useWarRoom()
  const [fetched, setFetched] = useState<ApprovalRequest[]>([])
  const [chainOk, setChainOk] = useState<boolean | null>(null)
  const [copied, setCopied] = useState<string | null>(null)
  const live = transport?.mode === 'live'
  // the store keeps only the cards it saw (and the last 20 decided); a page opened later reads them once from the server
  useEffect(() => {
    if (!live) return
    let alive = true
    apiGet<ApprovalRequest[]>(API.approvals).then((v) => { if (alive) setFetched(v) }, () => {})
    apiGet<{ audit_chain_ok?: boolean }>('/api/health/components').then((v) => { if (alive) setChainOk(v.audit_chain_ok ?? null) }, () => {})
    return () => { alive = false }
  }, [live, ev.id, ev.closed_at, ev.review?.status])
  const approvals = useMemo(() => {
    const by = new Map(fetched.map((a) => [a.id, a]))
    for (const a of [...state.resolved, ...state.pending]) by.set(a.id, a)
    return [...by.values()]
  }, [fetched, state.resolved, state.pending])
  const tasks = state.world?.tasks.items
  const report = useMemo(() => buildReport({ ev, approvals, tasks: tasks ?? [], chainOk, auditCount: null }), [ev, approvals, tasks, chainOk])
  const copy = () => {
    const done = (t: string) => { setCopied(t); setTimeout(() => setCopied(null), 3000) }
    try { navigator.clipboard.writeText(reportText(report, '')).then(() => done('已複製'), () => done('無法複製，請開啟完整報告')) } catch { done('無法複製，請開啟完整報告') }
  }
  const parts = report.parts.filter((p) => p.id !== 'audit' && p.rows.length)
  return (
    <section className="r-report" id={`report-${ev.id}`} data-testid="trace-report" data-event={ev.id} aria-label="處理報告">
      <AiReport ev={ev} />
      <header>
        <ToolIcon name="confirm_schedule" /><b>處理報告</b><small>由系統紀錄整理，不經 AI 撰寫</small>
        <a href={`/report?event=${encodeURIComponent(ev.id)}`} target="_blank" rel="noopener" data-testid="trace-report-link">開啟完整報告</a>
      </header>
      <h4>{report.title}</h4>
      <dl className="r-report-head">
        {report.head.map((h) => <div key={h.k}><dt>{h.k}</dt><dd className={h.tone ? `t-${h.tone}` : ''}>{h.text}</dd></div>)}
      </dl>
      {parts.map((p) => (
        <details key={p.id} open={p.id === 'actions' || p.id === 'tasks'}>
          <summary>{p.title}<small>{p.rows.length}</small></summary>
          <ul>{p.rows.map((r, i) => <li key={i} className={r.tone ? `t-${r.tone}` : ''}>{r.time && <time>{r.time}</time>}{r.k && <b>{r.k}</b>}<span>{r.text}</span></li>)}</ul>
        </details>
      ))}
      <p className="r-report-foot"><button type="button" className="btn-ghost" onClick={copy}>複製報告文字</button>{copied && <small role="status">{copied}</small>}</p>
    </section>
  )
}
