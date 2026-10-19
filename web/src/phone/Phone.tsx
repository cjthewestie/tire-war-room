// Supervisor phone (proposal page 11 card): approve / modify / reject over the factory LAN.
import { useState } from 'react'
import type { ApprovalRequest, AskAnswer, DecisionKind } from '../contracts'
import { STATE_LABEL } from '../components/common'
import { TemperatureTrend } from '../components/Trend'
import { RISK_PLAIN, plain } from '../plain'
import { machineLabel } from '../factory'
import { fmtHM, fmtHMS, fmtMD } from '../format'
import { primaryEvent } from '../store'
import { useWarRoom } from '../useWarRoom'
import { useApprovalAlerts } from './alert'

const ROUND = ['', '第一輪', '第二輪', '第三輪', '第四輪']

export function Phone() {
  const { state, transport, connected } = useWarRoom()
  const pending = [...state.pending].sort((a, b) => (a.created_at < b.created_at ? 1 : -1))
  const net = state.snapshot?.header.network ?? 'online'
  const lanDown = net === 'lan_down'
  const ev = primaryEvent(state.snapshot?.events)
  const ft = state.clock?.factory_time ?? state.snapshot?.header.factory_time
  const alerts = useApprovalAlerts(pending, connected && !lanDown)
  return (
    <div className="phone-wrap">
      <div className="phone">
        <div className="ph-status">
          <span>主管手機</span>
          <span className="muted">{ft ? `${fmtMD(ft)} ${fmtHM(ft)}` : ''}</span>
          <span className={`ph-conn ${connected && !lanDown ? 'ok' : 'bad'}`}>{lanDown ? 'LAN 中斷' : connected ? (transport?.mode === 'mock' ? '模擬' : '廠內 LAN') : '連線中'}</span>
        </div>
        {!alerts.armed ? (
          <button className="ph-arm" onClick={() => void alerts.arm()}>開啟核准提醒（聲音・震動{alerts.permission === 'unsupported' ? '' : '・通知'}）</button>
        ) : (
          <div className="ph-armed muted">核准提醒已開啟{alerts.permission === 'granted' ? '・系統通知' : ''}{alerts.lastAlert ? `・最近：${alerts.lastAlert}` : ''}</div>
        )}
        {alerts.held > 0 && <div className="ph-banner">{alerts.held} 件推播暫存中，網路恢復後補推。</div>}
        {lanDown && <div className="ph-banner">廠內 LAN 中斷：請至現場觸控螢幕核准（警示燈已亮）。</div>}
        {pending.length === 0 ? (
          <div className="ph-card idle">
            <h2>目前沒有待核准事項</h2>
            {ev ? (
              <p className="muted">最新事件：{machineLabel(ev.machine_id)}・{STATE_LABEL[ev.state]}{ev.outcome ? `・${plain(ev.outcome)}` : ''}</p>
            ) : <p className="muted">全廠正常，事件偵測常駐監看中。</p>}
            {state.resolved.slice(0, 3).map((r) => (
              <p key={r.id} className="ph-hist">{fmtHMS(r.decision?.decided_at ?? r.created_at)}・{plain(r.title)}・<b>{DEC_LABEL[r.status] ?? r.status}</b>{r.decision?.option_id ? `（${r.decision.option_id}）` : ''}</p>
            ))}
          </div>
        ) : pending.map((a) => <ApprovalCard key={a.id} a={a} disabled={lanDown || !transport} />)}
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
      <h2 className="ph-title">問戰情室</h2>
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

const DEC_LABEL: Record<string, string> = { approved: '已核准', modified: '已修改', rejected: '已駁回', expired: '逾時' }

function ApprovalCard({ a, disabled }: { a: ApprovalRequest; disabled: boolean }) {
  const { transport, state } = useWarRoom()
  const ev = state.snapshot?.events.find((e) => e.id === a.event_id) ?? null
  const [opt, setOpt] = useState<string | null>(null)
  const [mode, setMode] = useState<DecisionKind | null>(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const chosen = opt ?? (a.options.length === 1 ? a.options[0].id : null)
  // What 修改 can actually change: the option (a different staffing choice than the system's).
  // A free-text note never becomes tool args (every *Args model is extra='forbid'), so a modify that
  // picks no other option executes nothing: it goes back to the commander as a reject with the note,
  // and the state machine replans (pages 5/7: 主管 核准／修改／否決; nothing he refused may run).
  const base = (a.options.find((o) => o.recommended) ?? a.options[0])?.id ?? null
  const changesOption = chosen != null && chosen !== base
  const chosenOpt = a.options.find((o) => o.id === chosen)
  const send = async (decision: DecisionKind) => {
    if (!transport) return
    if (decision === 'reject' && !reason.trim()) { setMode('reject'); setErr('駁回需要填寫理由'); return }
    if (decision === 'modify' && !reason.trim()) { setMode('modify'); setErr('修改需要填寫修改內容'); return }
    if (decision === 'approve' && !chosen) { setErr('請先選擇方案'); return }
    const note = reason.trim()
    let d: { decision: DecisionKind; option_id: string | null; reason: string }
    if (decision === 'modify' && changesOption) {
      d = { decision: 'modify', option_id: chosen, reason: `修改：改選「${chosenOpt?.label ?? chosen}」；${note}` }
    } else if (decision === 'modify') {
      d = { decision: 'reject', option_id: null, reason: `退回修改：${note}（未改選方案，原方案不執行，請依修改內容重排）` }
    } else {
      d = { decision, option_id: decision === 'reject' ? null : chosen, reason: note || defaultReason(a, chosen) }
    }
    setBusy(true); setErr(null)
    try {
      await transport.decide({ request_id: a.id, ...d, modified_args: null, decided_by: '主管', via: 'phone' })
    } catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }
  return (
    <div className="ph-card ph-approval">
      <div className="ph-col-a">
      <div className="ph-top">
        <span className="ph-risk" title={a.risk}>{RISK_PLAIN[a.risk]}</span>
        <span className="ph-round">{ROUND[a.round] ?? `第${a.round}輪`}核准</span>
      </div>
      <h2 className="ph-title">{plain(a.title)}</h2>
      {ev && <TemperatureTrend machineId={ev.machine_id} hypotheses={ev.hypotheses} compact />}
      <dl className="ph-info">
        <dt>原因</dt><dd>{plain(a.reason)}</dd>
        <dt>已做</dt><dd>{a.done.length ? a.done.map(plain).join('；') : '—'}</dd>
        <dt>缺口</dt><dd>{a.gaps.length ? a.gaps.map(plain).join('；') : '無'}</dd>
        <dt>交期</dt><dd>{plain(a.delivery_impact)}</dd>
        {a.actions?.length ? (<>
          <dt>要核准</dt>
          <dd><ul className="ph-acts-list">{a.actions.map((x, i) => <li key={i}>{plain(x)}</li>)}</ul></dd>
        </>) : null}
      </dl>
      </div>
      <div className="ph-col-b">
      <p className="ph-q">選一個人力方案</p>
      <div className="ph-opts" role="radiogroup" aria-label="方案">
        {a.options.map((o) => (
          <button key={o.id} role="radio" aria-checked={chosen === o.id} disabled={disabled || busy}
            className={`ph-opt ${chosen === o.id ? 'on' : ''}`} onClick={() => setOpt(o.id)}>
            <b>{o.id}</b>　{o.label}{chosen === o.id ? '（主管選擇）' : o.recommended ? <small>系統建議</small> : null}
          </button>
        ))}
      </div>
      {mode && (
        <textarea className="ph-reason" placeholder={mode === 'reject' ? '駁回理由（必填）' : '修改內容（必填，寫入稽核日誌）'} value={reason} onChange={(e) => setReason(e.target.value)} rows={2} />
      )}
      {mode === 'modify' && (
        <p className="ph-modify-hint" data-testid="modify-hint">{changesOption
          ? `送出後執行：${chosen} ${chosenOpt?.label ?? ''}（取代系統建議）＋其餘核准內容。修改內容寫入稽核日誌，不改硫化時間等公式數值。`
          : '未改選方案：原方案全部不執行，修改內容退回指揮官重新規劃（記為退回）。要換人力方案，請先點選另一個方案。'}</p>
      )}
      {err && <p className="err">{err}</p>}
      <div className="ph-acts">
        {!chosen && !mode && <p className="ph-hint">請先選方案，再按核准</p>}
        <button className="ph-approve" disabled={disabled || busy || !chosen} onClick={() => send('approve')}>{busy ? '送出中…' : `核准${chosen ? ` ${chosen}` : ''}`}</button>
        <button className="ph-sec" disabled={disabled || busy} onClick={() => (mode === 'modify' ? send('modify') : setMode('modify'))}>{mode === 'modify' ? (changesOption ? `送出修改（改選 ${chosen}）` : '退回重排') : '修改'}</button>
        <button className="ph-sec danger" disabled={disabled || busy} onClick={() => (mode === 'reject' ? send('reject') : setMode('reject'))}>{mode === 'reject' ? '確認駁回' : '駁回'}</button>
      </div>
      <p className="ph-foot">決定與理由會寫入稽核日誌，系統接著往下執行。</p>
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
