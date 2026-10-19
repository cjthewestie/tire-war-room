// 產線監控 opens with 預警 (UX brief, 「平常巡一下」: 「有沒有快出事」 first): things not yet an event but heading the
// wrong way, from live data only — machines in 注意, materials under the reorder point, late purchase orders,
// maintenance starting soon, orders the schedule finishes late, readings the validation layer rejected.
import { machineLabel } from '../factory'
import { fmtHM, fmtMD, ms } from '../format'
import { plain } from '../plain'
import { useWarRoom } from '../useWarRoom'

const H = 3_600_000

export function EarlyWarnings({ onMachine }: { onMachine?: (id: string) => void }) {
  const { state } = useWarRoom()
  const now = ms(state.clock?.factory_time ?? state.snapshot?.header.factory_time)
  const w = state.world
  const rows: { k: string; what: string; when: string; machine?: string; tone: 'warn' | 'bad' }[] = []
  for (const m of state.snapshot?.machines ?? []) {
    if (m.status === 'warning') rows.push({ k: machineLabel(m.machine_id), what: `讀值偏離正常範圍${m.temp_c != null ? `（${m.temp_c.toFixed(1)}°C）` : ''}，事件偵測監看中`, when: '現在', machine: m.machine_id, tone: 'warn' })
  }
  for (const mt of w?.materials ?? []) {
    if (mt.status === 'ok') continue
    rows.push({ k: '原料', what: `${mt.name}剩 ${mt.stock.toLocaleString()} ${mt.unit}${mt.days_left != null ? `，約 ${mt.days_left} 天` : ''}${mt.status === 'reorder' ? '，低於再訂購點' : mt.status === 'low' ? '，低於安全量' : '，已用完'}`,
      when: mt.on_order > 0 ? `在途 ${mt.on_order.toLocaleString()} ${mt.unit}` : '尚未下單', tone: mt.status === 'reorder' ? 'warn' : 'bad' })
  }
  for (const po of w?.purchase_orders ?? []) if (po.status === '延遲') rows.push({ k: '採購', what: `${po.name} ${po.qty} ${po.unit}（${po.supplier}）延遲`, when: `原訂 ${fmtMD(po.eta)} ${fmtHM(po.eta)}`, tone: 'warn' })
  for (const m of w?.maintenance.upcoming ?? []) {
    const t = ms(m.start)
    if (t > now && t - now < 12 * H) rows.push({ k: machineLabel(m.machine_id), what: `${m.title}（停機保養／換模）`, when: `${fmtHM(m.start)} 開始`, machine: m.machine_id, tone: 'warn' })
  }
  for (const s of state.schedule?.schedule.warnings ?? []) rows.push({ k: '交期', what: plain(s), when: '排程預估', tone: 'warn' })
  for (const b of state.snapshot?.header.bad_data ?? []) rows.push({ k: machineLabel(b.machine_id), what: `驗證層攔下 ${b.count} 筆壞資料，未喚醒 AI`, when: fmtHM(b.last_at), machine: b.machine_id, tone: 'warn' })
  return (
    <section className="panel early" aria-label="預警" data-testid="early-warnings">
      <header className="panel-h"><h2>預警：還沒出事，但在往壞的方向走</h2><span className="muted small">{rows.length ? `${rows.length} 項` : ''}</span></header>
      {!rows.length && <p className="muted">目前沒有預警。機台、原料、採購、保養與交期都在範圍內。</p>}
      {rows.length > 0 && (
        <ul className="early-list">
          {rows.map((r, i) => (
            <li key={i} className={`tone-${r.tone}`}>
              {r.machine && onMachine ? <button type="button" className="early-k" onClick={() => onMachine(r.machine!)}>{r.k}</button> : <b className="early-k">{r.k}</b>}
              <span>{r.what}</span><span className="early-when">{r.when}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
