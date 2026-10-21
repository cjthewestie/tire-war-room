// 今天誰上班 (approved draft v2-simple.html): one row per team with today's shifts (red = short), and a last row with
// the moments someone must be at a press (red tick = de-mold, dark tick = mold-in). 06:00 today → 06:00 tomorrow,
// drawn at the real pixel width so a phone gets readable 12–13 px text instead of a shrunken 760 px picture.
import type { Shift } from '../../contracts'
import { LABOR_TEAMS, TEAM_LABELS } from '../../factory'
import { fmtHM, ms } from '../../format'
import { useWidth } from '../common'
import { gapOf, H, type Need } from './model'

/** Rough text width: CJK glyphs are one em, digits and latin about 0.6 em. */
const textW = (s: string, fs: number) => [...s].reduce((w, c) => w + (c.charCodeAt(0) > 0x2e80 ? fs : fs * 0.6), 0)

export function PeopleChart({ shifts, needs, day, now }: { shifts: Shift[]; needs: Need[]; day: number; now: number }) {
  const [ref, width] = useWidth<HTMLDivElement>()
  const W = Math.max(280, width)
  const narrow = W < 560
  const LABEL = narrow ? 100 : 132, ROW = narrow ? 36 : 40, BAR = 24, TOP = 28, FS = narrow ? 12.5 : 13
  const a0 = day + 6 * H, a1 = a0 + 24 * H
  const x = (t: number) => LABEL + ((Math.min(Math.max(t, a0), a1) - a0) / (a1 - a0)) * (W - LABEL - 4)
  const rows = LABOR_TEAMS.length
  const yNeed = TOP + rows * ROW
  const Hh = yNeed + ROW + 18
  const step = narrow ? 6 : 3
  const ticks = Array.from({ length: 24 / step + 1 }, (_, i) => a0 + i * step * H)
  const inDay = needs.filter((n) => n.t >= a0 && n.t < a1)
  const anyShort = shifts.some((s) => gapOf(s) > 0)
  const showNow = !isNaN(now) && now >= a0 && now <= a1
  return (
    <div ref={ref} className={`sp-chart ${narrow ? 'narrow' : ''}`}>
      <svg width={W} height={Hh} role="img" aria-label={`今天誰上班：${shifts.map((s) => `${TEAM_LABELS[s.team] ?? s.team}${s.name} ${s.headcount}${s.required != null ? `／${s.required}` : ''} 人`).join('、')}`}>
        {ticks.map((t, i) => {
          const last = i === ticks.length - 1
          return (
            <g key={t}>
              <line x1={x(t)} x2={x(t)} y1={TOP - 6} y2={yNeed + ROW - 4} className="pc-tk" />
              {/* the label before 「隔天 06:00」 would run into it */}
              {(narrow || i !== ticks.length - 2) && <text x={x(t)} y={14} textAnchor={last ? 'end' : 'middle'} className="pc-tkt">{last && !narrow ? `隔天 ${fmtHM(t)}` : fmtHM(t)}</text>}
            </g>
          )
        })}
        {LABOR_TEAMS.map((team, i) => {
          const y = TOP + i * ROW + (ROW - BAR) / 2
          return (
            <g key={team}>
              <text x={0} y={y + BAR / 2 + 5} className="pc-team">{TEAM_LABELS[team]}</text>
              <rect x={LABEL} y={y} width={W - LABEL - 4} height={BAR} rx={5} className="pc-off" />
              {shifts.filter((s) => s.team === team).map((s) => {
                const a = x(ms(s.start)), b = x(ms(s.end))
                const gap = gapOf(s)
                const full = gap ? `${s.name} ${s.headcount}／${s.required} 人・缺 ${gap}` : `${s.name} ${s.headcount} 人`
                const short = gap ? `缺 ${gap}` : `${s.headcount} 人`
                const label = textW(full, FS) + 14 <= b - a ? full : textW(short, FS) + 12 <= b - a ? short : ''
                return (
                  <g key={s.id}>
                    <rect x={a} y={y} width={Math.max(2, b - a)} height={BAR} rx={5} className={gap ? 'pc-short' : 'pc-on'}>
                      <title>{`${TEAM_LABELS[s.team] ?? s.team} ${s.name} ${fmtHM(s.start)}–${fmtHM(s.end)}・${s.headcount}${s.required != null ? `／${s.required}` : ''} 人${gap ? `，缺 ${gap} 人` : ''}`}</title>
                    </rect>
                    {label && <text x={a + 8} y={y + BAR / 2 + 5} className={`pc-bt ${gap ? 'on-short' : ''}`}>{label}</text>}
                  </g>
                )
              })}
            </g>
          )
        })}
        <text x={0} y={yNeed + ROW / 2 + 5} className={`pc-team ${anyShort ? 'alert' : ''}`}>要有人在場</text>
        {inDay.map((n, i) => (
          <line key={i} x1={x(n.t)} x2={x(n.t)} y1={yNeed + 6} y2={yNeed + ROW - 6} className={n.kind === 'demold' ? 'pc-dm' : 'pc-mi'}>
            <title>{`${fmtHM(n.t)} ${Number(n.job.press_id.slice(5))} 號機 ${n.kind === 'demold' ? '出模' : '入模'}`}</title>
          </line>
        ))}
        {showNow && (
          <g>
            <line x1={x(now)} x2={x(now)} y1={TOP - 4} y2={yNeed + ROW - 2} className="pc-now" />
            <text x={x(now) > W - 70 ? x(now) - 4 : x(now) + 4} y={Hh - 3} textAnchor={x(now) > W - 70 ? 'end' : 'start'} className="pc-nowt">現在 {fmtHM(now)}</text>
          </g>
        )}
      </svg>
      <div className="sp-legend">
        <span><i className="lg-on" />人數夠</span><span><i className="lg-short" />人不夠</span>
        <span><i className="lg-dm" />出模</span><span><i className="lg-mi" />入模</span>
        {!isNaN(now) && now < a0 && <span>現在 {fmtHM(now)}，今天的班從 06:00 開始</span>}
      </div>
    </div>
  )
}
