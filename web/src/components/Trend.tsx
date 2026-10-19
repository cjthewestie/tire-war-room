// ③ temperature trend for the event's machine: the page-11 story (128 → 122 → 119.5 °C) as a line,
// with the cure band, the 120 °C safety line and the commander's expectation for the hypothesis.
import type { Hypothesis } from '../contracts'
import { COMPOUND_STOP_C, CURATIVE_STOP_C, machineLabel } from '../factory'
import { fmtHM, ms } from '../format'
import { hypName } from '../plain'
import { useMachineDetail } from '../useMachineDetail'
import { useWidth } from './common'

export function TemperatureTrend({ machineId, hypotheses = [], compact = false }: { machineId: string; hypotheses?: Hypothesis[]; compact?: boolean }) {
  const d = useMachineDetail(machineId)
  const [ref, width] = useWidth<HTMLDivElement>()
  const h = (d?.history ?? []).filter((r) => r.quality === 'ok' && r.temp_c != null)
  const isPress = machineId.startsWith('CURE')
  const stage = d?.state.compound_stage
  const stopLine = isPress ? 120 : stage === 'curative_added' ? CURATIVE_STOP_C : stage === 'unvulcanized' ? COMPOUND_STOP_C : null
  const stopText = isPress ? '120°C 以下：安全核心自動保溫保壓' : stopLine != null ? `${stopLine}°C 以上：安全核心停機` : ''
  // the commander's expectation for the newest temperature hypothesis (page 11: H1 expects ≥121 °C within 30 min)
  const hyp = [...hypotheses].reverse().find((x) => x.expectation && x.expectation.channel === 'temp_c')
  const exp = hyp?.expectation ?? null
  const W = Math.max(280, width), H = compact ? 150 : 168
  const L = 34, R = 12, T = 10, B = 22
  if (h.length < 2) {
    return <div ref={ref} className="trend" data-testid="trend"><div className="trend-h"><span>{machineLabel(machineId)} 溫度（近 30 分）</span></div><div className="trend-empty" style={{ height: H }}>讀取中…</div></div>
  }
  const temps = h.map((r) => r.temp_c as number)
  let lo = Math.min(...temps), hi = Math.max(...temps)
  if (isPress) { lo = Math.min(lo, 118); hi = Math.max(hi, 130) }
  if (stopLine != null) { lo = Math.min(lo, stopLine - 2); hi = Math.max(hi, stopLine + 2) }
  if (exp) { lo = Math.min(lo, exp.value - 1); hi = Math.max(hi, exp.value + 1) }
  lo = Math.floor(lo); hi = Math.ceil(hi)
  const t0 = ms(h[0].ts), t1 = ms(h[h.length - 1].ts)
  const x = (t: number) => L + ((t - t0) / Math.max(1, t1 - t0)) * (W - L - R)
  const y = (v: number) => T + (1 - (v - lo) / Math.max(1, hi - lo)) * (H - T - B)
  const path = h.map((r, i) => `${i ? 'L' : 'M'}${x(ms(r.ts)).toFixed(1)},${y(r.temp_c as number).toFixed(1)}`).join('')
  const area = `${path}L${x(t1).toFixed(1)},${H - B}L${x(t0).toFixed(1)},${H - B}Z`
  const first = temps[0], last = temps[temps.length - 1]
  const below = stopLine != null && (isPress ? last < stopLine : last >= stopLine)
  const step = hi - lo > 9 ? 5 : 2
  const ticks: number[] = []
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) ticks.push(v)
  return (
    <div ref={ref} className={`trend ${below ? 'is-alarm' : ''}`} data-testid="trend">
      <div className="trend-h">
        <span>{machineLabel(machineId)} 溫度（近 30 分）</span>
        <b className="trend-delta"><span className="muted">{first.toFixed(1)}</span><i aria-hidden>→</i><small>現在</small>{last.toFixed(1)}°C</b>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img"
        aria-label={`${machineLabel(machineId)} 溫度由 ${first.toFixed(1)} 降到 ${last.toFixed(1)}°C`}>
        <defs>
          <linearGradient id="trend-fill" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="currentColor" stopOpacity=".28" />
            <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
          </linearGradient>
        </defs>
        {isPress && <rect x={L} y={y(130)} width={W - L - R} height={y(120) - y(130)} className="tr-band" />}
        {ticks.map((v) => (
          <g key={v}>
            <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} className="tr-grid" />
            <text x={L - 6} y={y(v) + 4} textAnchor="end" className="tr-axis">{v}</text>
          </g>
        ))}
        {stopLine != null && <line x1={L} x2={W - R} y1={y(stopLine)} y2={y(stopLine)} className="tr-stop" />}
        {exp && hyp && <line x1={L} x2={W - R} y1={y(exp.value)} y2={y(exp.value)} className={`tr-exp ${hyp.status}`} />}
        <path d={area} fill="url(#trend-fill)" className="tr-area" />
        <path d={path} className="tr-line" />
        <circle cx={x(t1)} cy={y(last)} r={4.5} className="tr-dot" />
        <text x={L} y={H - 5} className="tr-axis">{fmtHM(t0)}</text>
        <text x={W - R} y={H - 5} textAnchor="end" className="tr-axis">現在 {fmtHM(t1)}</text>
      </svg>
      <p className="trend-legend">
        {isPress && <span><i className="lg-band" />硫化範圍 120–130°C</span>}
        {stopLine != null && <span><i className="lg-stop" />{stopText}</span>}
        {exp && hyp && <span className={hyp.status === 'rejected' ? 'is-miss' : ''}><i className="lg-exp" />{hypName(hyp.id)}預期 {exp.within_min} 分內 {exp.op === '>=' ? '≥' : exp.op === '<=' ? '≤' : ''}{exp.value}°C{hyp.status === 'rejected' ? '・沒達到' : ''}</span>}
      </p>
    </div>
  )
}
