// 人力排程 「今天的人力」 (HY, 2026-10-21: 「界面看起來很簡單易懂，必要資訊就好；重複的資訊、主管不需要知道的資訊全部刪掉」):
// who is here, who is on leave, which shift is short, today's risks and the shift-load rows — from GET /api/shifts and GET /api/world.
// Today's mold-ins (→ 接下來入模 and 訂單), materials (→ 產線監控) and the last 12 h of events (→ 主控台、狀態機) live on those pages.
import { useEffect, useState } from 'react'
import type { Shift } from '../contracts'
import { TEAM_LABELS, machineLabel } from '../factory'
import { factoryMidnight, fmtHM, fmtMD, fmtWeekday, ms } from '../format'
import { plain } from '../plain'
import { useWarRoom } from '../useWarRoom'
import { ShiftLoad } from './ShiftLoad'
import './schedule.css'

const H = 3_600_000

export function TodayPlan() {
  const { state, transport } = useWarRoom()
  const now = ms(state.clock?.factory_time ?? state.snapshot?.header.factory_time)
  const d0 = isNaN(now) ? NaN : factoryMidnight(now)
  const [shifts, setShifts] = useState<Shift[]>([])
  useEffect(() => { transport?.shifts().then(setShifts).catch(() => {}) }, [transport, d0])
  const w = state.world
  const leave = (w?.people.workers ?? []).filter((p) => p.state === '請假')
  const short = shifts.filter((s) => ms(s.end) > d0 && ms(s.start) < d0 + 24 * H && s.required != null && s.required > s.headcount)
  const low = (w?.materials ?? []).filter((m) => m.status !== 'ok')
  const maint = (w?.maintenance.upcoming ?? []).filter((m) => ms(m.start) >= d0 && ms(m.start) < d0 + 24 * H)
  const risks = [...(state.schedule?.schedule.warnings ?? []).map(plain), ...maint.map((m) => `${fmtHM(m.start)} ${machineLabel(m.machine_id)}${m.title}`),
    ...low.map((m) => `${m.name}剩約 ${m.days_left ?? '—'} 天`)]
  return (
    <section className="panel today sp-people" aria-label="今天的人力" data-testid="today-plan">
      <header className="panel-h"><h2>今天的人力{isNaN(d0) ? '' : `・${fmtMD(d0)}（${fmtWeekday(d0)}）`}</h2></header>
      <div className="sp-stats">
        <div><span className="sp-k">在廠</span><b>{w?.people.on_duty ?? '—'}</b><small>／{w?.people.total ?? '—'} 人</small></div>
        <div><span className="sp-k">請假</span><b>{leave.length}</b><small>人</small>
          {leave.length > 0 && <em>{leave.map((p) => `${p.name}・${p.team_label}`).join('、')}</em>}</div>
        <div className={short.length ? 'is-short' : 'is-ok'}><span className="sp-k">缺人</span><b>{short.reduce((n, s) => n + s.required! - s.headcount, 0)}</b><small>人</small>
          <em>{short.length ? short.map((s) => `${TEAM_LABELS[s.team] ?? s.team} ${s.name}`).join('、') : '每一班都夠'}</em></div>
      </div>
      {risks.length > 0 && (
        <div className="sp-risks"><span className="sp-k">今天要注意</span><ul>{risks.map((r, i) => <li key={i}>{r}</li>)}</ul></div>
      )}
      {isNaN(d0) ? null : <ShiftLoad shifts={shifts} d0={d0} d1={d0 + 24 * H} />}
    </section>
  )
}
