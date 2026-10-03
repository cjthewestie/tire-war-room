import { useState } from 'react'
import type { ApprovalRequest, Event, FocusPanel, MachineTile, OrderProgress, PendingAction, SemiFinishedProgress } from '../contracts'
import { LINES, LINE_SHORT, machineLabel } from '../factory'
import { fmtHM, fmtNum } from '../format'
import { primaryEvent } from '../store'
import { useWarRoom } from '../useWarRoom'
import { Panel, STATE_LABEL, STATUS_LABEL, TimelineTable } from './common'

export function MonitorTab({ onMachine }: { onMachine: (id: string) => void }) {
  const { state } = useWarRoom()
  const s = state.snapshot
  if (!s) return <div className="loading">等待工廠資料…</div>
  const lanDown = s.header.network === 'lan_down'
  return (
    <div className="monitor">
      <div className="col-left">
        <OrdersPanel orders={s.orders} />
        <SemiPanel semi={s.semi_finished} />
      </div>
      <FocusPanelView focus={s.focus} pending={state.pending} lanDown={lanDown} />
      <MachinesPanel machines={s.machines} onMachine={onMachine} />
    </div>
  )
}

function OrdersPanel({ orders }: { orders: OrderProgress[] }) {
  return (
    <Panel num="①" title="訂單進度（完成／預計）" className="orders">
      {orders.map((o) => (
        <div className="order" key={o.size}>
          <div className="order-row">
            <span className="order-l">{o.label}<b>{o.done}</b><small> / {o.total}</small></span>
            <span className={o.note ? 'order-h moved' : 'muted'}>剩餘約 {fmtNum(o.remaining_h, 0)} 小時</span>
          </div>
          {o.note && <p className="order-note small">{o.note}</p>}
          <div className="bar" role="progressbar" aria-valuenow={o.done} aria-valuemax={o.total} aria-label={`${o.label}進度`}>
            <i style={{ width: `${(100 * o.done) / Math.max(1, o.total)}%` }} />
          </div>
        </div>
      ))}
    </Panel>
  )
}

function SemiPanel({ semi }: { semi: SemiFinishedProgress }) {
  return (
    <Panel num="②" title={`半成品進度（${semi.tire_label}）`} className="semi">
      <ul className="semi-list">
        {semi.parts.map((p) => (
          <li key={p.part}>
            <span>{p.label}</span>
            <span className={`semi-st ${p.state}`}>{p.status_text}</span>
          </li>
        ))}
      </ul>
      {semi.planned_mold_in_at && <p className="muted small">預計 {fmtHM(semi.planned_mold_in_at)} 入硫化機</p>}
      {semi.note && <p className={`small semi-note semi-st ${semi.note_state ?? ''}`}>{semi.note}</p>}
    </Panel>
  )
}

function FocusPanelView({ focus, pending, lanDown }: { focus: FocusPanel | null; pending: ApprovalRequest[]; lanDown: boolean }) {
  const { transport, state } = useWarRoom()
  const [sent, setSent] = useState<Record<string, string>>({})
  const [err, setErr] = useState<string | null>(null)
  if (!focus) {
    const ev = primaryEvent(state.snapshot?.events)
    return (
      <Panel num="③" title="中央說明面板" className="focus calm">
        <div className="calm-body">
          <p className="calm-title">全廠正常</p>
          <p className="muted">事件偵測常駐監看 28 台機台（非 LLM、全天候常駐）。異常 1 分鐘內喚醒指揮官。</p>
          {ev && <p className="muted small">上一事件：{machineLabel(ev.machine_id)}・{STATE_LABEL[ev.state]}{ev.outcome ? `・${ev.outcome}` : ''}</p>}
        </div>
      </Panel>
    )
  }
  const approve = async (p: PendingAction) => {
    if (!transport) return
    setErr(null)
    setSent((x) => ({ ...x, [p.approval_id]: p.option_id ?? 'A' }))
    try {
      await transport.decide({ request_id: p.approval_id, decision: 'approve', option_id: p.option_id, via: lanDown ? 'touchscreen' : 'dashboard', decided_by: '主管', reason: '戰情室核准' })
    } catch (e) { setErr(String(e)); setSent((x) => { const c = { ...x }; delete c[p.approval_id]; return c }) }
  }
  const req = (id: string) => pending.find((a) => a.id === id)
  const closed = focus.state === 'closed' || focus.state === 'safe_stop'
  const ev = state.snapshot?.events.find((e) => e.id === focus.event_id) ?? null
  const view = focusView(focus, ev, [...pending, ...state.resolved])
  return (
    <Panel num="③" title="中央說明面板" className={`focus ${closed ? 'closed' : 'hot'}`} right={<span className={`state-pill st-${focus.state}`}>{STATE_LABEL[focus.state]}</span>}>
      <h3 className="focus-title">{view.title}</h3>
      <p className="focus-detail">{view.detail}</p>
      {view.causes.length > 0 && (
        <ul className="focus-causes">
          {view.causes.map((c) => <li key={c.text} className={`fc-${c.kind}`}>{c.text}</li>)}
        </ul>
      )}
      <div className="tiles">
        {focus.tiles.map((t) => (
          <div className={`tile ${t.status}`} key={t.label}>
            <span className="tile-l">{t.label}</span>
            <span className="tile-v">{tileValue(t.label, t.value, focus.machine_id)}</span>
          </div>
        ))}
      </div>
      <h4 className="sub">建議調整</h4>
      <ul className="actions">
        {focus.auto_done.map((a) => <li key={a} className="done"><span className="ck">✓</span>已自動：{a}</li>)}
        {(focus.approved ?? []).map((a, i) => (
          <li key={`${a.approval_id}-${i}`} className="done approved">
            <span className="ck">✓</span>已核准：{a.label}<small className="muted">（{whoLabel(a.decided_by)}）</small>
          </li>
        ))}
        {focus.pending.map((p) => {
          const r = req(p.approval_id)
          const s = sent[p.approval_id]
          return (
            <li key={`${p.approval_id}-${p.option_id}`} className="todo">
              <span className="box" aria-hidden>☐</span>
              <span className="todo-l">待核准：{p.label}{r && <small className="risk">{r.risk}{r.round > 1 ? `・第${r.round}輪` : ''}</small>}</span>
              <button className="btn-approve" disabled={!!s} onClick={() => approve(p)}>{s === p.option_id ? '送出中…' : '核准'}</button>
            </li>
          )
        })}
      </ul>
      {err && <p className="err">核准失敗：{err}</p>}
      <p className="impact">排程影響：{focus.schedule_impact}</p>
      {focus.timeline.length > 0 && (
        <details className="focus-tl">
          <summary>事件時間軸（{focus.timeline.length}）</summary>
          <TimelineTable items={focus.timeline.slice(-6)} compact />
        </details>
      )}
    </Panel>
  )
}

function MachinesPanel({ machines, onMachine }: { machines: MachineTile[]; onMachine: (id: string) => void }) {
  return (
    <Panel num="④" title="機台狀態（依產線）" className="machines">
      <div className="mlines">
        {LINES.map((ln) => (
          <div className="mline" key={ln}>
            <span className={`mline-k ln-${ln}`}>{LINE_SHORT[ln]}</span>
            <div className="mline-items">
              {machines.filter((m) => m.line === ln).map((m) => (
                <button key={m.machine_id} className={`mdot st-${m.status}`} onClick={() => onMachine(m.machine_id)}
                  title={`${machineLabel(m.machine_id)}・${STATUS_LABEL[m.status]}${m.temp_c != null ? `・${m.temp_c}°C` : ''}`}
                  aria-label={`${machineLabel(m.machine_id)} ${STATUS_LABEL[m.status]}`} data-machine={m.machine_id}>
                  <i className="dot" />{m.short_name}
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
      <p className="muted small foot">點選機台：看溫度、壓力、時間、水流</p>
    </Panel>
  )
}

type Cause = { kind: 'hyp' | 'ok' | 'no'; text: string }
/** Rebuild ③'s title, detail and cause from the live event, so the panel never contradicts the tiles or the phone:
 *  the wake-time reading is labelled as such, the live value comes from the temperature tile, and a cause stays
 *  "假設 H1" until code verifies it. After a replan the round-2 approval title (e.g. 7 號機停用，重排中) leads. */
function focusView(focus: FocusPanel, ev: Event | null, reqs: ApprovalRequest[]): { title: string; detail: string; causes: Cause[] } {
  // Only the server-built panel freezes the wake-time anomaly message; a live-built detail (mock) is used as-is.
  if (!ev || !ev.anomaly.message || !focus.detail.startsWith(ev.anomaly.message)) return { title: focus.title, detail: focus.detail, causes: [] }
  const base = focus.detail.split('；原因：')[0]
  const temp = focus.tiles.find((t) => t.label === '溫度')
  const detail = `喚醒時（${fmtHM(ev.anomaly.ts)}）：${base}${temp && temp.value !== '—' ? `。現在：${temp.value}` : ''}`
  const rejected = ev.hypotheses.filter((h) => h.status === 'rejected')
  const latestRound = reqs.filter((r) => r.event_id === ev.id).sort((a, b) => b.round - a.round || (a.created_at < b.created_at ? 1 : -1))[0]
  const title = rejected.length > 0 && latestRound && latestRound.round > 1
    ? `${machineLabel(ev.machine_id)}：${latestRound.title}`
    : focus.title
  const causes: Cause[] = ev.hypotheses.map((h) => (
    h.status === 'confirmed' ? { kind: 'ok' as const, text: `原因（已驗證）${h.id}：${h.statement}` }
      : h.status === 'rejected' ? { kind: 'no' as const, text: `${h.id} 不成立：${h.statement}${h.evidence.length ? `（${h.evidence[h.evidence.length - 1]}）` : ''}` }
        : { kind: 'hyp' as const, text: `假設 ${h.id}（${ev.state === 'closed' || ev.state === 'safe_stop' ? '未驗證，已依 SOP 處置' : '驗證中'}）：${h.statement}` }
  ))
  if (!causes.length) {
    const c = focus.detail.split('；原因：')[1]
    if (c) causes.push({ kind: 'hyp', text: `假設：${c}（尚未驗證）` })
  }
  return { title, detail, causes }
}

/** "supervisor" is the API default for a phone decision; show the role in Chinese. */
function whoLabel(by: string): string {
  return !by || by === 'supervisor' ? '主管' : by
}

/** Presses cure for 24–34 h: show the server's minute count as hours. */
function tileValue(label: string, value: string, machineId: string): string {
  const m = label === '時間' && machineId.startsWith('CURE-') ? /^(\d+(?:\.\d+)?)分$/.exec(value) : null
  return m ? `${(Number(m[1]) / 60).toFixed(1)} h` : value
}
