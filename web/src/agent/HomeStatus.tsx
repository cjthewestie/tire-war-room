// One status line above the home regions (UX brief: 「平常巡一下」 answered in one glance): machines normal/abnormal,
// today's de-molds vs the schedule, the next truck out, people short on the current shift, pending approvals.
// Clicking it opens 產線監控 (data-testid=status-strip is kept for the e2e checks).
import { useEffect, useMemo, useState } from 'react'
import type { Shift } from '../contracts'
import { factoryMidnight, fmtHM, ms } from '../format'
import { useWarRoom } from '../useWarRoom'
import { ThemeToggle } from '../components/ThemeToggle'

const NET_SHORT: Record<string, string> = { online: '網路正常', weak: '弱網', wan_down: 'WAN 中斷', lan_down: 'LAN 中斷・現場操作' }
const H = 3_600_000

export function HomeStatus({ onMonitor }: { onMonitor?: () => void }) {
  const { state, transport } = useWarRoom()
  const h = state.snapshot?.header
  const now = ms(state.clock?.factory_time ?? h?.factory_time)
  const day = isNaN(now) ? 0 : Math.floor(now / (6 * H))
  const [shifts, setShifts] = useState<Shift[]>([])
  useEffect(() => { transport?.shifts().then(setShifts).catch(() => {}) }, [transport, day])
  const d0 = isNaN(now) ? NaN : factoryMidnight(now)
  const out = useMemo(() => {
    const today = (state.schedule?.schedule.jobs ?? []).filter((j) => ms(j.demold) >= d0 && ms(j.demold) < d0 + 24 * H)
    return { plan: today.length, done: today.filter((j) => ms(j.demold) <= now).length }
  }, [state.schedule, d0, now])
  const onNow = shifts.filter((s) => ms(s.start) <= now && ms(s.end) > now)
  const short = onNow.reduce((n, s) => n + Math.max(0, (s.required ?? s.headcount) - s.headcount), 0)
  const dep = state.world?.dock.next_departure
  const wait = state.world?.finished_goods.waiting_ship
  const pend = state.pending.length
  const net = h?.network ?? 'online'
  const abn = h?.abnormal_count ?? 0
  return (
    <div className="home-status-row">
    <button type="button" className="agent-strip home-status" onClick={onMonitor} data-testid="status-strip" title="打開產線監控">
      <span className="as-item ok"><i className="dot ok" />正常 {h?.normal_count ?? '–'} 台</span>
      <span className={`as-item ${abn > 0 ? 'bad' : ''}`}><i className={`dot ${abn > 0 ? 'bad' : 'idle'}`} />異常 {h?.abnormal_count ?? '–'} 台</span>
      <span className="as-item" title="今天排定出模的條數（目前生效排程）">今日出模 <b>{out.done} / {out.plan}</b> 條</span>
      {dep && <span className="as-item">下一車出貨 <b>{fmtHM(dep)}</b>{wait != null ? `・待出貨 ${wait} 條` : ''}</span>}
      {shifts.length > 0 && <span className={`as-item ${short ? 'warn' : ''}`}>{short ? `本班缺 ${short} 人` : '本班人力足'}</span>}
      <span className={`as-item ${pend ? 'warn' : ''}`}>待核准 {pend} 件</span>
      {net !== 'online' && <span className="as-item bad">{NET_SHORT[net] ?? net}</span>}
      <span className="as-go">產線監控 →</span>
    </button>
    <ThemeToggle />
    </div>
  )
}
