// Factory-time formatting. All API datetimes are ISO strings with a +08:00 offset (Asia/Taipei).
export const TZ = 'Asia/Taipei'

const hm = new Intl.DateTimeFormat('en-GB', { timeZone: TZ, hour: '2-digit', minute: '2-digit', hour12: false })
const hms = new Intl.DateTimeFormat('en-GB', {
  timeZone: TZ, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
})
const md = new Intl.DateTimeFormat('en-GB', { timeZone: TZ, month: '2-digit', day: '2-digit' })
const wd = new Intl.DateTimeFormat('zh-TW', { timeZone: TZ, weekday: 'short' })

export const ms = (iso: string | null | undefined): number => (iso ? new Date(iso).getTime() : NaN)

export function fmtHM(iso: string | number | null | undefined): string {
  if (iso == null) return '—'
  const d = typeof iso === 'number' ? new Date(iso) : new Date(iso)
  return isNaN(d.getTime()) ? '—' : hm.format(d)
}
export function fmtHMS(iso: string | number | null | undefined): string {
  if (iso == null) return '—'
  const d = new Date(iso)
  return isNaN(d.getTime()) ? '—' : hms.format(d)
}
/** "10/22" */
export function fmtMD(iso: string | number | null | undefined): string {
  if (iso == null) return '—'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return '—'
  const [dd, mm] = md.format(d).split('/')
  return `${mm}/${dd}`
}
export function fmtWeekday(iso: string | number): string {
  return wd.format(new Date(iso))
}

/** Factory-local hour of day (0–24, fractional) for an epoch ms. */
export function hourOfDay(t: number): number {
  const parts = hms.formatToParts(new Date(t))
  const h = Number(parts.find((p) => p.type === 'hour')?.value ?? 0) % 24
  const m = Number(parts.find((p) => p.type === 'minute')?.value ?? 0)
  const s = Number(parts.find((p) => p.type === 'second')?.value ?? 0)
  return h + m / 60 + s / 3600
}

/** Epoch ms of local (+08:00) midnight for the factory day containing t. */
export function factoryMidnight(t: number): number {
  return t - hourOfDay(t) * 3_600_000
}

/** ISO 8601 with +08:00 offset, like the Python API emits. */
export function isoTaipei(t: number): string {
  const d = new Date(t + 8 * 3_600_000)
  const p = (n: number, w = 2) => String(n).padStart(w, '0')
  return (
    `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}` +
    `T${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}+08:00`
  )
}

export function fmtNum(n: number | null | undefined, digits = 1): string {
  if (n == null || isNaN(n)) return '—'
  return n.toLocaleString('en-US', { maximumFractionDigits: digits, minimumFractionDigits: 0 })
}
