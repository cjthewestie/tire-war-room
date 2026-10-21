// One order: due vs the schedule's last de-mold, per-stage progress (投料 → 貼合 → 入模硫化 → 出模 → 完成 → 出貨),
// where its tyres are now and what blocks it. Used by 訂單 and by a 交期風險 item on the home screen.
import type { ScheduleJob, WorldOrder } from '../contracts'
import { SIZE_LABEL, machineLabel } from '../factory'
import { fmtHM, fmtMD, ms } from '../format'
import { useWarRoom } from '../useWarRoom'
import { orderView } from './orderModel'

const H = 3_600_000

export function OrderCard({ o, jobs, now, warnings }: { o: WorldOrder; jobs: ScheduleJob[]; now: number; warnings: string[] }) {
  const { state } = useWarRoom()
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
  return (
    <section className="panel order-card" data-testid="order-card" aria-label={`訂單 ${o.id}`}>
      <h2 className="order-title">{o.id}・{o.customer}・{SIZE_LABEL[o.size]} {o.qty} 條{o.rush && <em className="tag rush">急單</em>}</h2>
      <p className="order-eta">
        交期 <b>{fmtMD(o.due)}</b>
        {isFinite(eta) && <>・最後一條預計 <b>{fmtMD(eta)} {fmtHM(eta)}</b> 出模・<b className={late || warnings.length ? 'late' : 'ontime'}>{late ? `會晚 ${lateH} h` : warnings.length ? (n < o.qty - o.done ? `其餘 ${o.qty - o.done - n} 條預估晚交` : '預估晚交') : '準時'}</b></>}
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
      <div className="order-grid">
        <div>
          <h3 className="sub">現在在哪</h3>
          {inMold.length ? <ul className="w-list">{inMold.map((j) => <li key={j.id}>{machineLabel(j.press_id)}硫化中・{fmtMD(j.demold)} {fmtHM(j.demold)} 出模（剩 {Math.max(0, Math.round((ms(j.demold) - now) / H))} h）</li>)}</ul>
            : <p className="muted">{nextUp ? `下一條 ${fmtMD(nextUp.mold_in)} ${fmtHM(nextUp.mold_in)} 入 ${machineLabel(nextUp.press_id)}` : o.status}</p>}
        </div>
        <div>
          <h3 className="sub">卡在哪</h3>
          {blockers.length ? <ul className="w-list">{blockers.map((b, i) => <li key={i} className="warn">{b}</li>)}</ul> : <p className="muted">無</p>}
        </div>
      </div>
    </section>
  )
}
