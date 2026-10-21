// One order: due vs the schedule's last de-mold, per-stage progress (投料 → 貼合 → 入模硫化 → 出模 → 完成 → 出貨),
// where its tyres are now and what blocks it. Used by 訂單 and by a 交期風險 item on the home screen.
// On 訂單 (`actions`) it also shows the basic-info block and the 列印訂單 / 修改訂單 buttons: print uses the browser's own
// window.print() with a print-only sheet of this one order (index.css @media print); edit (訂單變更申請) is a front-end-only preview. 列印訂單 is hidden on phones by CSS only.
import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import type { ScheduleJob, WorldOrder } from '../contracts'
import { SIZE_LABEL, machineLabel } from '../factory'
import { TZ, fmtHM, fmtMD, ms } from '../format'
import { useWarRoom } from '../useWarRoom'
import { OrderEditDialog } from './OrderEditDialog'
import { orderView } from './orderModel'

const H = 3_600_000
const stamp = (d: Date) => d.toLocaleString('zh-TW', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit', hour: 'numeric', minute: '2-digit', hour12: true })

export function OrderCard({ o, jobs, now, warnings, actions = false }: { o: WorldOrder; jobs: ScheduleJob[]; now: number; warnings: string[]; actions?: boolean }) {
  const { state } = useWarRoom()
  const [printing, setPrinting] = useState<Date | null>(null)
  const [editing, setEditing] = useState(false)
  useEffect(() => {
    if (!printing) return
    document.body.classList.add('printing-order')
    const done = () => setPrinting(null)
    window.addEventListener('afterprint', done)
    const t = window.setTimeout(() => window.print(), 60)
    return () => { window.clearTimeout(t); window.removeEventListener('afterprint', done); document.body.classList.remove('printing-order') }
  }, [printing])
  const v = orderView(o, jobs, now, warnings, state.world?.goods?.orders.find((g) => g.order_id === o.id))
  const sorted = v.jobs
  const passed = (t: string) => isFinite(now) && ms(t) <= now
  const n = sorted.length
  const shipped = o.status === '已出貨'
  const { stages, eta, lateH } = v
  const late = lateH != null
  const inMold = sorted.filter((j) => passed(j.mold_in) && !passed(j.demold))
  const nextUp = sorted.find((j) => !passed(j.mold_in))
  const outOfService = new Set((state.snapshot?.machines ?? []).filter((m) => m.status === 'out_of_service').map((m) => m.machine_id))
  const held = sorted.filter((j) => outOfService.has(j.press_id) && !passed(j.demold))
  const blockers = [
    ...warnings,
    ...held.map((j) => `${machineLabel(j.press_id)}停用：這條扣模待品管`),
    ...(!n && !shipped && o.status !== '完成' && o.status !== '待出貨' ? ['還沒排進硫化排程（目前排程視窗內沒有這張單）'] : []),
  ]
  const where = inMold.length ? inMold.map((j) => `${machineLabel(j.press_id)}硫化中，${fmtMD(j.demold)} ${fmtHM(j.demold)} 出模`).join('；')
    : nextUp ? `下一條 ${fmtMD(nextUp.mold_in)} ${fmtHM(nextUp.mold_in)} 入 ${machineLabel(nextUp.press_id)}` : o.status
  return (
    <section className="panel order-card" data-testid="order-card" aria-label={`訂單 ${o.id}`}>
      <div className="order-head">
        <h2 className="order-title">{o.id}・{o.customer}・{SIZE_LABEL[o.size]} {o.qty} 條{o.rush && <em className="tag rush">急單</em>}</h2>
        {actions && (
          <div className="oc-actions">
            <button type="button" className="btn-ghost oc-btn oc-print" data-testid="order-print" onClick={() => setPrinting(new Date())}>
              <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round"><path d="M6 8V3h8v5M6 14H3.5V8h13v6H14M6 12h8v5H6z" /></svg>列印訂單
            </button>
            <button type="button" className="btn-ghost oc-btn" data-testid="order-edit-open" onClick={() => setEditing(true)}>
              <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round"><path d="M13.5 3.5l3 3L7 16l-4 1 1-4z" /></svg>修改訂單
            </button>
          </div>
        )}
      </div>
      {actions && (
        <dl className="oc-info" aria-label="訂單基本資訊">
          <div><dt>訂單編號</dt><dd>{o.id}</dd></div>
          <div><dt>客戶</dt><dd>{o.customer}</dd></div>
          <div><dt>胎型</dt><dd>{SIZE_LABEL[o.size]}</dd></div>
          <div><dt>訂購數量</dt><dd>{o.qty} 條</dd></div>
          <div><dt>承諾交期</dt><dd>{fmtMD(o.due)}</dd></div>
          <div><dt>優先級</dt><dd>{o.rush ? '急單' : '一般'}</dd></div>
        </dl>
      )}
      <p className="order-eta">
        交期 <b>{fmtMD(o.due)}</b>
        {isFinite(eta) && <>・已排程 {n} 條最後一條出模 <b>{fmtMD(eta)} {fmtHM(eta)}</b>・<b className={late || warnings.length ? 'late' : 'ontime'}>{late ? `會晚 ${lateH} h` : warnings.length ? (n < o.qty - o.done ? `其餘 ${o.qty - o.done - n} 條預估晚交` : '預估晚交') : '準時'}</b></>}
        {!isFinite(eta) && <>・{o.status}</>}
      </p>
      <ol className="order-stages" aria-label="各站進度">
        {stages.map((s) => {
          const pct = s.of ? Math.round((s.v / s.of) * 100) : 0
          return (
            <li key={s.k} className={pct >= 100 ? 'done' : pct > 0 ? 'part' : ''}>
              <span className="os-k">{s.k}</span>
              <span className="os-bar" aria-hidden><i style={{ width: `${pct}%` }} /></span>
              <span className="os-v">{s.of ? `${s.v}/${s.of}` : '—'}</span>
            </li>
          )
        })}
      </ol>
      <p className="muted small">投料到出模依目前生效排程第 {state.schedule?.version ?? '—'} 版中這張單的 {n} 條計算；完成與出貨依工廠紀錄。</p>
      <div className="order-grid">
        <div>
          <h3 className="sub">現在在哪</h3>
          {actions && <p className="oc-prog">已完成 <b>{o.done}</b>／{o.qty} 條・剩餘 <b>{Math.max(0, o.qty - o.done)}</b> 條</p>}
          {inMold.length ? <ul className="w-list">{inMold.map((j) => <li key={j.id}>{machineLabel(j.press_id)}硫化中・{fmtMD(j.demold)} {fmtHM(j.demold)} 出模（剩 {Math.max(0, Math.round((ms(j.demold) - now) / H))} h）</li>)}</ul>
            : <p className="muted">{nextUp ? `下一條 ${fmtMD(nextUp.mold_in)} ${fmtHM(nextUp.mold_in)} 入 ${machineLabel(nextUp.press_id)}` : o.status}</p>}
        </div>
        <div>
          <h3 className="sub">卡點</h3>
          {blockers.length ? <ul className="w-list">{blockers.map((b, i) => <li key={i} className="warn">{b}</li>)}</ul> : <p className="muted">無</p>}
        </div>
      </div>
      {editing && <OrderEditDialog o={o} onClose={() => setEditing(false)} />}
      {printing && createPortal(
        <div className="print-sheet" data-testid="print-sheet">
          <h1>訂單 {o.id}</h1>
          <p className="ps-time">列印時間：{stamp(printing)}</p>
          <table className="ps-info"><tbody>
            <tr><th>訂單編號</th><td>{o.id}</td><th>客戶</th><td>{o.customer}</td></tr>
            <tr><th>胎型</th><td>{SIZE_LABEL[o.size]}</td><th>訂購數量</th><td>{o.qty} 條</td></tr>
            <tr><th>承諾交期</th><td>{fmtMD(o.due)}</td><th>優先級</th><td>{o.rush ? '急單' : '一般'}</td></tr>
            <tr><th>生產狀態</th><td>{o.status}</td><th>完成數量</th><td>{o.done}／{o.qty} 條（剩餘 {Math.max(0, o.qty - o.done)} 條）</td></tr>
          </tbody></table>
          <h2>六站製程進度</h2>
          <table className="ps-stages"><thead><tr>{stages.map((s) => <th key={s.k}>{s.k}</th>)}</tr></thead>
            <tbody><tr>{stages.map((s) => <td key={s.k}>{s.of ? `${s.v}/${s.of}` : '—'}</td>)}</tr></tbody></table>
          <h2>目前狀態</h2>
          <p>{where}</p>
          <h2>卡點及備註</h2>
          {blockers.length ? <ul>{blockers.map((b, i) => <li key={i}>{b}</li>)}</ul> : <p>卡點：無</p>}
          <p>備註：—</p>
        </div>,
        document.body,
      )}
    </section>
  )
}
