// Shift capacity bars (DESIGN.md, raised line "J-card"): one row per shift of the day, in timetable column
// rhythm — 班組｜班別｜時段｜人力 bar. The bar fills with the people on shift against what the shift needs;
// a shortfall shows past the fill as a red hatched overflow ("缺 n"), never only as a colour.
import type { Shift } from '../contracts'
import { TEAM_LABELS } from '../factory'
import { fmtHM, ms } from '../format'

const H = 3_600_000

export function ShiftLoad({ shifts, d0, d1 }: { shifts: Shift[]; d0: number; d1: number }) {
  const rows = shifts
    .filter((s) => ms(s.end) > d0 && ms(s.start) < d1)
    .sort((a, b) => (a.team === b.team ? ms(a.start) - ms(b.start) : a.team < b.team ? -1 : 1))
  if (!rows.length) return null
  const scale = Math.max(1, ...rows.map((s) => Math.max(s.headcount, s.required ?? 0)))
  return (
    <div className="shift-load" data-testid="shift-load">
      <div className="sl-row sl-head" aria-hidden><span>班組</span><span>班別</span><span>時段</span><span>人力（在班／需要）</span></div>
      {rows.map((s) => {
        const need = s.required ?? s.headcount
        const short = Math.max(0, need - s.headcount)
        const hours = Math.round((ms(s.end) - ms(s.start)) / H)
        return (
          <div key={s.id} className={`sl-row ${short ? 'is-short' : ''}`}>
            <span className="sl-team">{TEAM_LABELS[s.team] ?? s.team}</span>
            <span>{s.name}</span>
            <span className="sl-time">{fmtHM(s.start)}–{fmtHM(s.end)}<small>{hours} h</small></span>
            <span className="sl-bar-cell">
              <span className="sl-bar" role="img" aria-label={`${s.headcount} 人在班，需要 ${need} 人${short ? `，缺 ${short} 人` : ''}`}>
                <i className="sl-fill" style={{ width: `${(Math.min(s.headcount, need) / scale) * 100}%` }} />
                {s.headcount > need && <i className="sl-extra" style={{ width: `${((s.headcount - need) / scale) * 100}%` }} />}
                {short > 0 && <i className="sl-short" style={{ width: `${(short / scale) * 100}%` }} />}
              </span>
              <b className="sl-n">{s.headcount}{s.required != null ? `／${s.required}` : ''}{short > 0 && <em>缺 {short}</em>}</b>
            </span>
          </div>
        )
      })}
    </div>
  )
}
