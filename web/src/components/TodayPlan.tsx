// 人力排程 opens with 「今日計劃」 (UX brief, 「早上開工／交班」): what starts today, who is here, materials, what happened
// overnight, today's risks and the shift-load bars — all from the active schedule, GET /api/shifts and GET /api/world.
// There is no API to approve a whole day's plan yet, so this card has no 核准 button (listed as a gap in the PR).
import { useEffect, useMemo, useState } from 'react'
import type { ScheduleJob, Shift } from '../contracts'
import { SIZE_LABEL, machineLabel } from '../factory'
import { factoryMidnight, fmtHM, fmtMD, fmtWeekday, ms } from '../format'
import { plain } from '../plain'
import { useWarRoom } from '../useWarRoom'
import { STATE_LABEL } from './common'
import { ShiftLoad } from './ShiftLoad'

const H = 3_600_000

export function TodayPlan() {
  const { state, transport } = useWarRoom()
  const now = ms(state.clock?.factory_time ?? state.snapshot?.header.factory_time)
  const d0 = isNaN(now) ? NaN : factoryMidnight(now)
  const [shifts, setShifts] = useState<Shift[]>([])
  useEffect(() => { transport?.shifts().then(setShifts).catch(() => {}) }, [transport, d0])
  const w = state.world
  const starts = useMemo(() => {
    const by = new Map<string, { n: number; presses: Set<string>; size: ScheduleJob['size'] }>()
    for (const j of state.schedule?.schedule.jobs ?? []) {
      const t = ms(j.mold_in)
      if (!(t >= d0 && t < d0 + 24 * H)) continue
      const g = by.get(j.order_id) ?? { n: 0, presses: new Set<string>(), size: j.size }
      g.n++; g.presses.add(j.press_id); by.set(j.order_id, g)
    }
    return [...by.entries()]
  }, [state.schedule, d0])
  const leave = (w?.people.workers ?? []).filter((p) => p.state === '請假')
  const overnight = (state.snapshot?.events ?? []).filter((e) => ms(e.opened_at) > now - 12 * H)
  const low = (w?.materials ?? []).filter((m) => m.status !== 'ok')
  const maint = (w?.maintenance.upcoming ?? []).filter((m) => ms(m.start) >= d0 && ms(m.start) < d0 + 24 * H)
  const risks = [...(state.schedule?.schedule.warnings ?? []).map(plain), ...maint.map((m) => `${fmtHM(m.start)} ${machineLabel(m.machine_id)}${m.title}`),
    ...low.map((m) => `${m.name}約 ${m.days_left ?? '—'} 天`)]
  const due = (id: string) => w?.orders.items.find((o) => o.id === id)?.due
  const customer = (id: string) => w?.orders.items.find((o) => o.id === id)?.customer
  const evRow = (e: (typeof overnight)[number]) => (
    <li key={e.id}>{fmtHM(e.opened_at)} {e.source === 'supervisor' ? '主管指令' : machineLabel(e.machine_id)}：{plain(e.alert_card?.title ?? e.command ?? e.anomaly.message)}（{STATE_LABEL[e.state]}）</li>
  )
  return (
    <section className="panel today" aria-label="今日計劃" data-testid="today-plan">
      <header className="panel-h"><h2>今日計劃・{isNaN(d0) ? '' : `${fmtMD(d0)}（${fmtWeekday(d0)}）`}</h2><span className="muted small">排程第 {state.schedule?.version ?? '—'} 版</span></header>
      <div className="today-grid">
        <div>
          <h3 className="sub">今天入模的單</h3>
          {starts.length ? (
            <table className="impact-t"><thead><tr><th>訂單</th><th>條數</th><th>硫化機</th><th>交期</th></tr></thead>
              {/* order numbers, not machines (CJ, ScheduleTab SchedHelp): each carries its tyre size and customer */}
              <tbody>{starts.map(([id, g]) => <tr key={id}><td><b>{id}</b> {SIZE_LABEL[g.size]}{customer(id) && <small className="muted">・{customer(id)}</small>}</td><td>{g.n}</td><td>{[...g.presses].map((p) => `${Number(p.slice(5))} 號`).join('、')}</td><td>{due(id) ? fmtMD(due(id)!) : '—'}</td></tr>)}</tbody></table>
          ) : <p className="muted">今天沒有排入模。</p>}
        </div>
        <div>
          <h3 className="sub">人</h3>
          <p className="today-line">在廠 <b>{w?.people.on_duty ?? '—'}</b> / {w?.people.total ?? '—'} 人・請假 <b>{leave.length}</b>{leave.length ? `（${leave.map((p) => `${p.name}・${p.team_label}`).join('、')}）` : ''}</p>
          <h3 className="sub">原料</h3>
          <p className="today-line">{(w?.materials ?? []).map((m) => `${m.name} ${m.days_left ?? '—'} 天`).join('・') || '—'}</p>
        </div>
      </div>
      <div className="today-grid">
        <div>
          <h3 className="sub">過去 12 小時發生的事</h3>
          {/* UX declutter: at most 3 here, the rest behind 「還有 N 件」 (same pattern as 預警) */}
          {overnight.length ? <ul className="w-list">{overnight.slice(0, 3).map(evRow)}</ul> : <p className="muted">沒有事件。</p>}
          {overnight.length > 3 && (
            <details className="tp-more"><summary>還有 {overnight.length - 3} 件</summary><ul className="w-list">{overnight.slice(3).map(evRow)}</ul></details>
          )}
        </div>
        <div>
          <h3 className="sub">今天的風險</h3>
          {risks.length ? <ul className="w-list">{risks.map((r, i) => <li key={i} className="warn">{r}</li>)}</ul> : <p className="muted">沒有。</p>}
        </div>
      </div>
      <h3 className="sub">今天各班負荷（在班人數／需要人數）</h3>
      {isNaN(d0) ? null : <ShiftLoad shifts={shifts} d0={d0} d1={d0 + 24 * H} />}
    </section>
  )
}
