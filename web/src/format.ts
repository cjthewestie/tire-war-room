// Factory-time formatting. All API datetimes are ISO strings with a +08:00 offset (Asia/Taipei).
export const TZ = 'Asia/Taipei'

const hms = new Intl.DateTimeFormat('en-GB', {
  timeZone: TZ, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
})
const wd = new Intl.DateTimeFormat('zh-TW', { timeZone: TZ, weekday: 'short' })

export const ms = (iso: string | null | undefined): number => (iso ? new Date(iso).getTime() : NaN)

// Display rule (issue #100): every time is 12-hour with AM/PM ("07:00 AM"), every date is year/month/day
// ("2026/10/21"). hms stays 24-hour: hourOfDay() and other code that does arithmetic on the parts need it.
const hm12d = new Intl.DateTimeFormat('en-US', { timeZone: TZ, hour: '2-digit', minute: '2-digit', hour12: true })
const hms12d = new Intl.DateTimeFormat('en-US', { timeZone: TZ, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true })
const ymd = new Intl.DateTimeFormat('en-GB', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit' })
const clean = (s: string) => s.replace(/[\u202f\u00a0]/g, ' ')

/** "07:00 AM" */
export function fmtHM(iso: string | number | null | undefined): string {
  if (iso == null) return '—'
  const d = new Date(iso)
  return isNaN(d.getTime()) ? '—' : clean(hm12d.format(d))
}
/** "07:00:05 AM" */
export function fmtHMS(iso: string | number | null | undefined): string {
  if (iso == null) return '—'
  const d = new Date(iso)
  return isNaN(d.getTime()) ? '—' : clean(hms12d.format(d))
}
/** "2026/10/22" (the name is from when it was month/day only) */
export function fmtMD(iso: string | number | null | undefined): string {
  if (iso == null) return '—'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return '—'
  const [dd, mm, yy] = ymd.format(d).split('/')
  return `${yy}/${mm}/${dd}`
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

/** A tool call's duration: 840 ms, 2.3 s. */
export function fmtMs(n: number): string { return n >= 1000 ? `${(n / 1000).toFixed(1)} s` : `${Math.round(n)} ms` }

/** kept for existing callers: fmtHM is 12-hour now */
export const fmtHM12 = fmtHM
