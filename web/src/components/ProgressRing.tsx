// Small SVG progress ring (issue #17): a tyre's status ring, a worker task's progress. value 0..1.
// tone picks the colour from the existing tokens (work.css); restyle there.
import './work.css'

export type RingTone = 'idle' | 'live' | 'warn' | 'alarm' | 'done'

export function ProgressRing({ value, size = 28, stroke = 3, tone = 'live', label, title }: {
  value: number; size?: number; stroke?: number; tone?: RingTone; label?: string; title?: string
}) {
  const v = Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0))
  const r = (size - stroke) / 2
  const c = 2 * Math.PI * r
  return (
    <svg className={`pring pring-${tone}`} width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img"
      aria-label={title ?? `${Math.round(v * 100)}%`} data-value={v.toFixed(3)}>
      {title && <title>{title}</title>}
      <circle className="pring-bg" cx={size / 2} cy={size / 2} r={r} strokeWidth={stroke} fill="none" />
      <circle className="pring-fg" cx={size / 2} cy={size / 2} r={r} strokeWidth={stroke} fill="none"
        strokeDasharray={`${c * v} ${c}`} transform={`rotate(-90 ${size / 2} ${size / 2})`} strokeLinecap="round" />
      {label && <text className="pring-t" x="50%" y="50%" dominantBaseline="central" textAnchor="middle">{label}</text>}
    </svg>
  )
}
