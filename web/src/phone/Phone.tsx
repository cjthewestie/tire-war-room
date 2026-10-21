// Supervisor phone (UX brief §6, CJ 2026-10-20): notification → ONE decision card → 核准, or 討論 to change it by
// talking (「agent 計劃就只有核准跟討論」) → done. Plus 問一句. Nothing else.
// #73: the card (and, once approved, the idle card) shows 修復預估: how long, how, and by when (RepairEta).
// Fast repair (Kyle 10-21): an event repaired and closed waits for 覆核 — a 「維修完成，請覆核」 card on top (ReviewCard),
// one per event (3 件同時異常: one after another), ringing like a new approval; 「已覆核」 joins the history.
import { useState } from 'react'
import type { ApprovalRequest, AskAnswer, Event } from '../contracts'
import { STATE_LABEL } from '../components/common'
import { TemperatureTrend } from '../components/Trend'
import { RISK_PLAIN, plain } from '../plain'
import { MACHINE_META, machineLabel } from '../factory'
import { fmtHM, fmtHMS, fmtMD } from '../format'
import { primaryEvent } from '../store'
import { useWarRoom } from '../useWarRoom'
import { reviewAlerts, useApprovalAlerts } from './alert'
import { ThemeToggle } from '../components/ThemeToggle'
import { RepairEta } from './RepairEta'
import { AiStatusLine } from '../AiStatusLine'
import { OptionGuideNote } from '../OptionGuideNote'
import { ReviewCard } from './ReviewCard'

const ROUND = ['', '第一輪', '第二輪', '第三輪', '第四輪']

export function Phone() {
  const { state, transport, connected } = useWarRoom()
  // the most urgent card first: R3 before R2, then the newest
  const pending = [...state.pending].sort((a, b) => (a.risk === b.risk ? (a.created_at < b.created_at ? 1 : -1) : a.risk > b.risk ? -1 : 1))
  const [pick, setPick] = useState<string | null>(null)
  const top = pending.find((p) => p.id === pick) ?? pending[0]
  // kept here, not in the card: the card is replaced by its new version once the agent has replanned
  const [sent, setSent] = useState<string | null>(null)
  const net = state.snapshot?.header.network ?? 'online'
  const lanDown = net === 'lan_down'
  const ev = primaryEvent(state.snapshot?.events)
  // the idle card: one status line per open event (3 at most, 「3 件同時異常」), the 修復預估 for the newest only
  const openEvs = (state.snapshot?.events ?? []).filter((e) => e.state !== 'closed' && e.state !== 'safe_stop')
    .sort((a, b) => (a.opened_at < b.opened_at ? 1 : -1)).slice(0, 3)
  // 覆核: confirmed here are hidden at once (the event update follows over the socket); keyed by the review request,
  // since a scenario restart reuses event ids
  const [reviewed, setReviewed] = useState<Record<string, Event>>({})
  const rkey = (e: Event) => `${e.id}:${e.review?.requested_at ?? ''}`
  const events = state.snapshot?.events ?? []
  const reviews = events.filter((e) => e.review?.status === 'pending' && !reviewed[rkey(e)])
    .sort((a, b) => (a.review!.requested_at < b.review!.requested_at ? -1 : 1))
  const doneReviews = events.filter((e) => e.review?.status === 'done' || reviewed[rkey(e)])
    .map((e) => (e.review?.status === 'done' ? e : reviewed[rkey(e)]))
  // a card the server carried over (decision.via 'auto', 「沿用 …」) repeats the supervisor's own decision line; 已覆核 rows
  // join it by time
  const history = [
    ...state.resolved.filter((r) => r.decision?.via !== 'auto').slice(0, 3).map((r) => ({ at: r.decision?.decided_at ?? r.created_at, key: r.id, node: (
      <>{plain(r.title)}{r.round > 1 ? `（${ROUND[r.round] ?? `第${r.round}輪`}）` : ''}・<b>{DEC_LABEL[r.status] ?? r.status}</b>{r.decision?.option_id ? `（${r.decision.option_id}）` : ''}</>
    ) })),
    ...doneReviews.map((e) => ({ at: e.review?.done_at ?? e.closed_at ?? e.opened_at, key: `rv-${e.id}`, node: (
      <>{machineLabel(e.machine_id)}維修完成・<b data-testid="phone-reviewed">已覆核</b></>
    ) })),
  ].sort((a, b) => (a.at < b.at ? 1 : -1)).slice(0, 6)
  const ft = state.clock?.factory_time ?? state.snapshot?.header.factory_time
  const alerts = useApprovalAlerts([...pending, ...reviewAlerts(reviews)], connected && !lanDown)
  return (
    <div className="phone-wrap">
      <div className="phone">
        <div className="ph-status">
          <span>主管手機</span>
          <span className="muted">{ft ? `${fmtMD(ft)} ${fmtHM(ft)}` : ''}</span>
          <span className={`ph-conn ${connected && !lanDown ? 'ok' : 'bad'}`}>{lanDown ? 'LAN 中斷' : connected ? (transport?.mode === 'mock' ? '模擬' : '廠內 LAN') : '連線中'}</span>
          <ThemeToggle className="ph-tog" />
        </div>
        {top ? <div className="ph-notif" role="status" data-testid="phone-notif"><b>戰情室・待你核准</b>{plain(top.title)}</div>
          : reviews.length > 0 && <div className="ph-notif ph-notif-ok" role="status" data-testid="phone-notif"><b>戰情室・維修完成，請覆核</b>{reviews.map((e) => machineLabel(e.machine_id)).join('、')}</div>}
        {!alerts.enabled ? (
          <button className="ph-arm" data-testid="phone-alerts-on" onClick={() => { alerts.setEnabled(true); void alerts.arm() }}>開啟核准提醒（聲音・震動{alerts.permission === 'unsupported' ? '' : '・通知'}）</button>
        ) : (
          <div className="ph-armed muted" data-testid="phone-alerts">
            <span>核准提醒已開啟{alerts.armed ? '' : '・點一下畫面啟用聲音'}{alerts.permission === 'granted' ? '・系統通知' : ''}{alerts.lastAlert ? `・最近：${alerts.lastAlert}` : ''}</span>
            <button className="ph-arm-off" onClick={(e) => { e.stopPropagation(); alerts.setEnabled(false) }}>關閉</button>
          </div>
        )}
        {alerts.enabled && alerts.blocked && <p className="ph-blocked small" role="status" data-testid="phone-alerts-blocked">{alerts.blocked}</p>}
        {sent && <p className="ph-sent" role="status">已送出「{sent}」・AI 重排中，新版核准卡會送到這裡</p>}
        {alerts.held > 0 && <div className="ph-banner">{alerts.held} 件推播暫存中，網路恢復後補推。</div>}
        {lanDown && <div className="ph-banner">廠內 LAN 中斷：請至現場觸控螢幕核准（警示燈已亮）。</div>}
        {reviews.map((e) => <ReviewCard key={rkey(e)} ev={e} disabled={lanDown || !transport}
          onDone={(x) => setReviewed((r) => ({ ...r, [rkey(e)]: x.review?.status === 'done' ? x : { ...e, review: { ...e.review!, status: 'done', done_at: ft ?? null, by: '主管' } } }))} />)}
        {pending.length === 0 ? (
          <div className="ph-card idle">
            <h2>目前沒有待核准事項</h2>
            {ev ? (
              <p className="muted">最新事件：{machineLabel(ev.machine_id)}・{STATE_LABEL[ev.state]}{ev.outcome ? `・${plain(ev.outcome)}` : ''}</p>
            ) : <p className="muted">全廠正常，事件偵測常駐監看中。</p>}
            {openEvs.length ? openEvs.map((e) => <AiStatusLine key={e.id} ev={e} className="ph-ai" />) : ev && <AiStatusLine ev={ev} className="ph-ai" />}
            {openEvs[0] && <RepairEta ev={openEvs[0]} />}
            {history.map((h) => <p key={h.key} className="ph-hist">{fmtHMS(h.at)}・{h.node}</p>)}
          </div>
        ) : <>
          <ApprovalCard key={top.id} a={top} disabled={lanDown || !transport} onSent={setSent} />
          {pending.length > 1 && (
            <div className="ph-more" data-testid="phone-more">
              <b>還有 {pending.length - 1} 件待核准</b>
              {pending.filter((p) => p.id !== top.id).map((p) => <button key={p.id} className="ph-more-i" onClick={() => setPick(p.id)}>{RISK_PLAIN[p.risk]}・{plain(p.title)}</button>)}
            </div>
          )}
        </>}
        {transport?.ask && <AskBox disabled={lanDown} />}
      </div>
    </div>
  )
}
const ASK_EXAMPLES = ['7 號機現在怎樣？', '今晚夜班缺誰？', '大開煉機過熱怎麼處理？']
const TOOL_ZH: Record<string, string> = {
  read_sensors: '讀感測器', predict_anomaly: '異常預測', cure_formula: '硫化公式',
  query_orders_shifts: '訂單／班表', sop_search: 'SOP',
}

// 問戰情室: read-only. The answer comes only from the read tools; anything that would change the
// plant is refused by the server and shown here as 已攔下.
function AskBox({ disabled }: { disabled: boolean }) {
  const { transport } = useWarRoom()
  const [q, setQ] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [answers, setAnswers] = useState<AskAnswer[]>([])
  const send = async (question: string) => {
    if (!transport?.ask || !question.trim()) return
    setBusy(true); setErr(null)
    try {
      const a = await transport.ask(question.trim())
      setAnswers((xs) => [a, ...xs].slice(0, 3)); setQ('')
    } catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }
  return (
    <div className="ph-card ph-ask" data-testid="ask-box">
      <h2 className="ph-title">問一句</h2>
      <p className="muted small">只查不改：答案只來自感測器、訂單班表與 SOP，提問會寫入稽核日誌。</p>
      <div className="ph-opts">
        {ASK_EXAMPLES.map((x) => (
          <button key={x} className="ph-opt" disabled={disabled || busy} onClick={() => void send(x)}>{x}</button>
        ))}
      </div>
      <textarea className="ph-reason" rows={2} maxLength={300} placeholder="例如：9 號機下一模是哪張訂單？"
        value={q} disabled={disabled || busy} onChange={(e) => setQ(e.target.value)} />
      {err && <p className="err">{err}</p>}
      <div className="ph-acts">
        <button className="ph-approve" disabled={disabled || busy || !q.trim()} onClick={() => void send(q)}>{busy ? '查詢中…' : '送出問題'}</button>
      </div>
      {answers.map((a) => (
        <div key={a.id} className="ph-answer" data-testid="ask-answer">
          <p className="ph-hist"><b>問</b>　{a.question}</p>
          <p><b>答</b>　{a.answer}</p>
          <p className="muted small">{a.id}・{a.steps.map((s) => `${TOOL_ZH[s.tool] ?? s.tool}${s.ok ? '' : '（失敗）'}`).join('、') || '未查詢'}
            {a.intercepted.length ? `・已攔下 ${a.intercepted.length}` : ''}{a.fell_back_to_mock ? '・規則模式備援' : ''}</p>
        </div>
      ))}
    </div>
  )
}

const DEC_LABEL: Record<string, string> = { approved: '已核准', modified: '已修改', rejected: '已駁回', expired: '逾時', superseded: '已改版（討論後由新版取代）' }

function ApprovalCard({ a, disabled, onSent }: { a: ApprovalRequest; disabled: boolean; onSent: (text: string) => void }) {
  const { transport, state } = useWarRoom()
  const ev = state.snapshot?.events.find((e) => e.id === a.event_id) ?? null
  const [opt, setOpt] = useState<string | null>(null)
  const [talk, setTalk] = useState(false)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const base = (a.options.find((o) => o.recommended) ?? a.options[0])?.id ?? null
  const chosen = opt ?? base
  // 核准 executes the chosen option (the system's unless the supervisor picked another one)
  const approve = async () => {
    if (!transport) return
    if (a.options.length > 0 && !chosen) { setErr('請先選擇方案'); return }
    setBusy(true); setErr(null)
    try {
      await transport.decide({ request_id: a.id, decision: 'approve', option_id: chosen, reason: defaultReason(a, chosen), modified_args: null, decided_by: '主管', via: 'phone' })
    } catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }
  // 討論: changing or refusing the plan is said in words; the agent replans and the card comes back as a new version
  const discuss = async () => {
    if (!transport?.discuss || !text.trim()) return
    setBusy(true); setErr(null)
    try { const t = text.trim(); onSent(t); setText(''); setTalk(false); await transport.discuss(a.event_id, t) } catch (e) { setErr(`送出失敗：${String(e)}`) } finally { setBusy(false) }
  }
  return (
    <div className="ph-card ph-approval">
      <div className="ph-col-a">
      <div className="ph-top">
        <span className="ph-risk" title={a.risk}>{RISK_PLAIN[a.risk]}</span>
        <span className="ph-round">{ROUND[a.round] ?? `第${a.round}輪`}核准</span>
        {(a.version ?? 1) > 1 && <span className="ph-round ph-ver" data-testid="phone-version">v{a.version}（最新）</span>}
      </div>
      <h2 className="ph-title">{plain(a.title)}</h2>
      <p className="ph-problem">{plain(a.reason)}</p>
      {(a.version ?? 1) > 1 && (
        <div className="ph-changed" data-testid="phone-changes">
          <b>與 v{(a.version ?? 2) - 1} 相比：</b>{(a.changes ?? []).length ? (a.changes ?? []).map(plain).join('；') : '排程內容相同'}
          {(a.constraints ?? []).length > 0 && <div className="muted">主管條件：{(a.constraints ?? []).join('、')}</div>}
        </div>
      )}
      <AiStatusLine ev={ev} className="ph-ai" />
      <dl className="ph-info">
        {a.actions?.length ? (<>
          <dt>建議</dt>
          <dd><ul className="ph-acts-list">{a.actions.map((x, i) => <li key={i}>{plain(x)}</li>)}</ul></dd>
        </>) : null}
        <dt>交期</dt><dd>{plain(a.delivery_impact)}</dd>
        {a.gaps.length > 0 && <><dt>缺口</dt><dd>{a.gaps.map(plain).join('；')}</dd></>}
      </dl>
      <RepairEta ev={ev} />
      <details className="ph-why">
        <summary>為什麼</summary>
        {a.done.length > 0 && <p className="muted small">已做：{a.done.map(plain).join('；')}</p>}
        {/* plant events and supervisor commands are about the whole plant (machine PLANT): no sensor trend to draw */}
        {ev && ev.machine_id in MACHINE_META && <TemperatureTrend machineId={ev.machine_id} hypotheses={ev.hypotheses} compact />}
      </details>
      </div>
      <div className="ph-col-b">
      {a.options.length > 1 && <>
        <p className="ph-q">選一個人力方案</p>
        <OptionGuideNote a={a} ev={ev} className="ph-guide" />
        <div className="ph-opts" role="radiogroup" aria-label="方案">
          {a.options.map((o) => (
            <button key={o.id} role="radio" aria-checked={chosen === o.id} disabled={disabled || busy}
              className={`ph-opt ${chosen === o.id ? 'on' : ''}`} onClick={() => setOpt(o.id)}>
              <b>{o.id}</b>　{o.label}{o.recommended ? <small>系統建議</small> : null}
            </button>
          ))}
        </div>
      </>}
      {talk && (
        <textarea className="ph-reason" rows={2} maxLength={300} autoFocus value={text} onChange={(e) => setText(e.target.value)}
          placeholder="例如：不要加班／9 號機今晚要保養／這個不行，改用 B" aria-label="跟 AI 討論這個計劃" data-testid="phone-discuss-input" />
      )}
      {err && <p className="err">{err}</p>}
      <div className="ph-acts">
        <button className="ph-approve" disabled={disabled || busy || (a.options.length > 0 && !chosen)} onClick={() => void approve()}>{busy ? '送出中…' : `核准${chosen && a.options.length > 1 ? `（${chosen}）` : ''}`}</button>
        <button className="ph-sec" disabled={disabled || busy || !transport?.discuss} onClick={() => (talk ? void discuss() : setTalk(true))} data-testid="phone-discuss">{talk ? '送出' : '討論'}</button>
      </div>
      <p className="ph-foot">要改或不同意，按「討論」跟 AI 說；決定寫入稽核日誌。</p>
      </div>
    </div>
  )
}

function defaultReason(a: ApprovalRequest, opt: string | null): string {
  const o = a.options.find((x) => x.id === opt)
  const rec = a.options.find((x) => x.recommended)
  if (o && rec && o.id !== rec.id) return `否決「${rec.label}」，改選「${o.label}」`
  return o ? `核准「${o.label}」` : ''
}
