import { useState } from 'react'
import type { ApprovalRequest, FocusPanel, MachineTile, OrderProgress, PendingAction, SemiFinishedProgress } from '../contracts'
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
            <span className="muted">剩餘約 {fmtNum(o.remaining_h, 0)} 小時</span>
          </div>
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
          <p className="muted">事件偵測常駐監看 28 台機台（非 LLM、always on）。異常 1 分鐘內喚醒指揮官。</p>
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
  return (
    <Panel num="③" title="中央說明面板" className={`focus ${closed ? 'closed' : 'hot'}`} right={<span className={`state-pill st-${focus.state}`}>{STATE_LABEL[focus.state]}</span>}>
      <h3 className="focus-title">{focus.title}</h3>
      <p className="focus-detail">{focus.detail}</p>
      <div className="tiles">
        {focus.tiles.map((t) => (
          <div className={`tile ${t.status}`} key={t.label}>
            <span className="tile-l">{t.label}</span>
            <span className="tile-v">{t.value}</span>
          </div>
        ))}
      </div>
      <h4 className="sub">建議調整</h4>
      <ul className="actions">
        {focus.auto_done.map((a) => <li key={a} className="done"><span className="ck">✓</span>已自動：{a}</li>)}
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
