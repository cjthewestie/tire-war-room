// Supervisor phone (proposal page 11 card): approve / modify / reject over the factory LAN.
import { useState } from 'react'
import type { ApprovalRequest, DecisionKind } from '../contracts'
import { STATE_LABEL } from '../components/common'
import { machineLabel } from '../factory'
import { fmtHM, fmtHMS, fmtMD } from '../format'
import { primaryEvent } from '../store'
import { useWarRoom } from '../useWarRoom'

const ROUND = ['', '第一輪', '第二輪', '第三輪', '第四輪']

export function Phone() {
  const { state, transport, connected } = useWarRoom()
  const pending = [...state.pending].sort((a, b) => (a.created_at < b.created_at ? 1 : -1))
  const net = state.snapshot?.header.network ?? 'online'
  const lanDown = net === 'lan_down'
  const ev = primaryEvent(state.snapshot?.events)
  const ft = state.clock?.factory_time ?? state.snapshot?.header.factory_time
  return (
    <div className="phone-wrap">
      <div className="phone">
        <div className="ph-status">
          <span>主管手機</span>
          <span className="muted">{ft ? `${fmtMD(ft)} ${fmtHM(ft)}` : ''}</span>
          <span className={`ph-conn ${connected && !lanDown ? 'ok' : 'bad'}`}>{lanDown ? 'LAN 中斷' : connected ? (transport?.mode === 'mock' ? '模擬' : '廠內 LAN') : '連線中'}</span>
        </div>
        {lanDown && <div className="ph-banner">廠內 LAN 中斷：請至現場觸控螢幕核准（警示燈已亮）。</div>}
        {pending.length === 0 ? (
          <div className="ph-card idle">
            <h2>目前沒有待核准事項</h2>
            {ev ? (
              <p className="muted">最新事件：{machineLabel(ev.machine_id)}・{STATE_LABEL[ev.state]}{ev.outcome ? `・${ev.outcome}` : ''}</p>
            ) : <p className="muted">全廠正常，事件偵測常駐監看中。</p>}
            {state.resolved.slice(0, 3).map((r) => (
              <p key={r.id} className="ph-hist">{fmtHMS(r.decision?.decided_at ?? r.created_at)}・{r.title}・<b>{DEC_LABEL[r.status] ?? r.status}</b>{r.decision?.option_id ? `（${r.decision.option_id}）` : ''}</p>
            ))}
          </div>
        ) : pending.map((a) => <ApprovalCard key={a.id} a={a} disabled={lanDown || !transport} />)}
      </div>
    </div>
  )
}
const DEC_LABEL: Record<string, string> = { approved: '已核准', modified: '已修改', rejected: '已駁回', expired: '逾時' }

function ApprovalCard({ a, disabled }: { a: ApprovalRequest; disabled: boolean }) {
  const { transport } = useWarRoom()
  const [opt, setOpt] = useState<string | null>(null)
  const [mode, setMode] = useState<DecisionKind | null>(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const chosen = opt ?? (a.options.length === 1 ? a.options[0].id : null)
  const send = async (decision: DecisionKind) => {
    if (!transport) return
    if (decision === 'reject' && !reason.trim()) { setMode('reject'); setErr('駁回需要填寫理由'); return }
    if (decision === 'modify' && !reason.trim()) { setMode('modify'); setErr('修改需要填寫修改內容'); return }
    if (decision !== 'reject' && !chosen) { setErr('請先選擇方案'); return }
    setBusy(true); setErr(null)
    try {
      await transport.decide({
        request_id: a.id, decision, option_id: decision === 'reject' ? null : chosen,
        // The note is free text for the audit log, never tool args: every *Args model is extra='forbid',
        // so merging {note} into the chosen step made the gate reject it and the action was silently dropped.
        modified_args: null,
        reason: decision === 'modify' ? `修改：${reason.trim()}` : (reason.trim() || defaultReason(a, chosen)),
        decided_by: '主管', via: 'phone',
      })
    } catch (e) { setErr(String(e)) } finally { setBusy(false) }
  }
  return (
    <div className="ph-card">
      <div className="ph-top">
        <span className="ph-risk">{a.risk}</span>
        <span className="ph-round">{ROUND[a.round] ?? `第${a.round}輪`}核准</span>
      </div>
      <h2 className="ph-title">{a.title}</h2>
      <dl className="ph-info">
        <dt>原因</dt><dd>{a.reason}</dd>
        <dt>已做</dt><dd>{a.done.length ? a.done.join('；') : '—'}</dd>
        <dt>缺口</dt><dd>{a.gaps.length ? a.gaps.join('；') : '無'}</dd>
        <dt>交期</dt><dd>{a.delivery_impact}</dd>
        {a.actions?.length ? (<>
          <dt>核准內容</dt>
          <dd>{a.actions.map((x, i) => <div key={i}>{x}</div>)}</dd>
        </>) : null}
      </dl>
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
      {err && <p className="err">{err}</p>}
      <div className="ph-acts">
        <button className="ph-approve" disabled={disabled || busy || !chosen} onClick={() => send('approve')}>{busy ? '送出中…' : `核准${chosen ? ` ${chosen}` : ''}`}</button>
        <button className="ph-sec" disabled={disabled || busy} onClick={() => (mode === 'modify' ? send('modify') : setMode('modify'))}>{mode === 'modify' ? '送出修改' : '修改'}</button>
        <button className="ph-sec danger" disabled={disabled || busy} onClick={() => (mode === 'reject' ? send('reject') : setMode('reject'))}>{mode === 'reject' ? '確認駁回' : '駁回'}</button>
      </div>
      <p className="ph-foot">決定與理由會寫入稽核日誌，回到狀態機繼續執行。</p>
    </div>
  )
}

function defaultReason(a: ApprovalRequest, opt: string | null): string {
  const o = a.options.find((x) => x.id === opt)
  const rec = a.options.find((x) => x.recommended)
  if (o && rec && o.id !== rec.id) return `否決「${rec.label}」，改選「${o.label}」`
  return o ? `核准「${o.label}」` : ''
}
