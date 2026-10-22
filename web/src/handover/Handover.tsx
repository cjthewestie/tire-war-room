// Issue #49: /handover, the shift handover page. Everything on it is built by code (build.ts) from the events,
// approvals, the world feed and the audit chain check; one button copies it as plain text for the LINE group.
import { useEffect, useMemo, useState } from 'react'
import { getJSON } from '../api'
import { API, type ApprovalRequest, type Event } from '../contracts'
import { useWarRoom } from '../useWarRoom'
import { buildHandover } from './build'
import { PendingApprovalAlert } from '../components/PendingApprovalAlert'

const HOURS = [8, 12, 24]
interface Daily { name: string; label: string; sentences: string[]; source: 'model' | 'code'; tasks: number; carried_over: string[] }
const POLL_MS = 5000

export function Handover() {
  const { state, connected } = useWarRoom()
  const [hours, setHours] = useState(8)
  const [events, setEvents] = useState<Event[]>([])
  const [approvals, setApprovals] = useState<ApprovalRequest[]>([])
  const [chainOk, setChainOk] = useState<boolean | null>(null)
  const [daily, setDaily] = useState<Daily[]>([])
  const [copied, setCopied] = useState<'ok' | 'manual' | null>(null)

  useEffect(() => {
    let live = true
    const pull = () => {
      getJSON<Event[]>(API.events).then((v) => live && setEvents(v)).catch(() => {})
      getJSON<ApprovalRequest[]>(API.approvals).then((v) => live && setApprovals(v)).catch(() => {})
      getJSON<{ audit_chain_ok?: boolean }>('/api/health/components').then((v) => live && setChainOk(v.audit_chain_ok ?? null)).catch(() => {})
      getJSON<{ daily: Daily[] }>('/api/memory/daily?limit=2').then((v) => live && setDaily(v.daily)).catch(() => {})
    }
    pull()
    const id = setInterval(pull, POLL_MS)
    return () => { live = false; clearInterval(id) }
  }, [])

  const now = state.world?.factory_time ?? state.clock?.factory_time ?? state.snapshot?.header.factory_time ?? null
  const h = useMemo(() => (now ? buildHandover({ now, hours, events, approvals, world: state.world, chainOk }) : null),
    [now, hours, events, approvals, state.world, chainOk])

  const copy = () => {
    if (!h) return
    const manual = () => setCopied('manual')
    try { navigator.clipboard.writeText(h.text).then(() => setCopied('ok'), manual) } catch { manual() }
    setTimeout(() => setCopied(null), 4000)
  }

  return (
    <div className="ho">
      <header className="ho-top">
        <PendingApprovalAlert variant="inline" newTab />
        <div className="ho-row">
          <h1>交班報告</h1>
          <span className={`ho-conn ${connected ? 'ok' : ''}`}>{connected ? '即時連線' : '連線中…'}</span>
        </div>
        <div className="ho-row">
          <div className="ho-seg" role="group" aria-label="範圍">
            {HOURS.map((x) => <button key={x} type="button" aria-pressed={hours === x} onClick={() => setHours(x)}>最近 {x} 小時</button>)}
          </div>
          <button className="ho-copy" type="button" onClick={copy} disabled={!h} data-testid="ho-copy">複製交班文字</button>
        </div>
        {copied === 'ok' && <p className="ho-note" role="status">已複製，可以直接貼到群組。</p>}
        {copied === 'manual' && <p className="ho-note" role="status">這個瀏覽器不讓網頁複製，請選取下方文字手動複製。</p>}
      </header>

      {!h && <p className="ho-empty">讀取工廠時間中…</p>}
      {h && <p className={`ho-lead t-${h.lead.tone}`} data-testid="ho-lead">{h.lead.text}</p>}
      {h && h.sections.map((s) => (
        <section key={s.id} className="ho-sec" data-testid={`ho-${s.id}`}>
          <h2>{s.title}<span>{s.summary}</span></h2>
          {s.lines.length > 0 && (
            <ul>
              {s.lines.map((l, i) => (
                <li key={i} className={l.tone ? `t-${l.tone}` : ''}>{l.time && <time>{l.time}</time>}<span>{l.text}</span></li>
              ))}
            </ul>
          )}
        </section>
      ))}
      {daily.length > 0 && (
        <section className="ho-sec" data-testid="ho-daily">
          <h2>前一天的每日摘要<span>{daily[0].label}・{daily[0].tasks} 件任務{daily[0].carried_over.length ? `・${daily[0].carried_over.length} 件轉到下一天` : ''}</span></h2>
          <ul><li><span>{daily[0].sentences.join('')}<small className="ho-src">{daily[0].source === 'model' ? 'Agent Nemo 依程式整理的事實寫成' : '程式整理'}・由稽核紀錄產生</small></span></li></ul>
        </section>
      )}
      {h?.foot && <p className="ho-foot" data-testid="ho-audit">{h.foot}</p>}
      {h && (
        <details className="ho-raw" open={copied === 'manual'}>
          <summary>交班文字（純文字）</summary>
          <textarea readOnly value={h.text} data-testid="ho-text" />
        </details>
      )}
    </div>
  )
}
