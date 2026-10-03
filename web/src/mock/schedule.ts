// Mock cure-press schedule (page 6 rules, simplified): press-centric, de-mold pushed into staffed windows.
import type { LaborNeed, Schedule, ScheduleJob, ScheduleVersion, Shift, StaffingChange, TireSize } from '../contracts'
import { CURE_HOURS, CURE_PRESS_IDS, OPTIMISED_SHIFTS } from '../factory'
import { factoryMidnight, hourOfDay, isoTaipei } from '../format'

export const HOUR = 3_600_000
export const MIN = 60_000

const BACKPROP: Record<TireSize, { bond: number; feed: number; pieces: number }> = {
  S: { bond: 40 / 60, feed: 100 / 60, pieces: 6 },
  M: { bond: 1, feed: 2.5, pieces: 8 },
  L: { bond: 1.5, feed: 3.5, pieces: 10 },
}

export function pressSize(id: string): TireSize {
  if (id === 'CURE-07' || id === 'CURE-08') return 'M'
  if (id === 'CURE-09' || id === 'CURE-10') return 'L'
  return 'S'
}

/** Weekday 0=Mon..6=Sun in factory time. */
function weekday(t: number): number {
  const d = new Date(t + 8 * HOUR)
  return (d.getUTCDay() + 6) % 7
}

/** Staffed for de-mold / mold-in (page 6): curing 06–23, body night 20–05; Mon–Sat, Sat night runs to Sun 05:00. */
export function staffed(t: number): boolean {
  const h = hourOfDay(t)
  const wd = weekday(t)
  if (wd === 6) return h < 5            // Sunday: only the tail of Saturday's night shift
  if (wd === 0 && h < 6) return false   // Monday early: no Sunday night shift
  return !(h >= 5 && h < 6)
}

export function nextStaffed(t: number): number {
  let x = t
  for (let i = 0; i < 24 * 4 * 3 && !staffed(x); i++) x = Math.ceil((x + 1) / (15 * MIN)) * 15 * MIN
  return x
}

function labor(job: { size: TireSize; mold_in: number; demold: number; bond_start: number; a_feed: number }): LaborNeed[] {
  const nightOrCuring = (t: number) => {
    const h = hourOfDay(t)
    return h >= 6 && h < 23 ? 'curing' : 'body'
  }
  return [
    { team: nightOrCuring(job.mold_in), task: 'mold_in', start: isoTaipei(job.mold_in), end: isoTaipei(job.mold_in + 30 * MIN), people: 2 },
    { team: nightOrCuring(job.demold), task: 'demold', start: isoTaipei(job.demold), end: isoTaipei(job.demold + 30 * MIN), people: 2 },
    { team: 'bond', task: 'bonding', start: isoTaipei(job.bond_start), end: isoTaipei(job.mold_in), people: 2 },
    { team: 'body', task: 'a_feed', start: isoTaipei(job.a_feed), end: isoTaipei(job.bond_start), people: 1 },
  ] as LaborNeed[]
}

let seqCounter = 0
export function makeJob(press: string, size: TireSize, moldIn: number, cureH: number, orderId: string, moldId: string, align = true): ScheduleJob {
  const rawDemold = moldIn + cureH * HOUR
  const demold = align ? nextStaffed(rawDemold) : rawDemold
  const bp = BACKPROP[size]
  const bond = moldIn - bp.bond * HOUR
  const feed = moldIn - bp.feed * HOUR
  return {
    id: `J-${press.slice(5)}-${(++seqCounter).toString().padStart(4, '0')}`,
    order_id: orderId, size, press_id: press, mold_id: moldId,
    mold_in: isoTaipei(moldIn), demold: isoTaipei(demold), cure_h: cureH,
    bond_start: isoTaipei(bond), a_feed_start: isoTaipei(feed),
    side_pieces: bp.pieces, bead_pieces: bp.pieces,
    labor: labor({ size, mold_in: moldIn, demold, bond_start: bond, a_feed: feed }),
    frozen: false,
  }
}

const SIZE_PREFIX: Record<TireSize, string> = { S: 'S-1', M: 'M-2', L: 'L-3' }

/**
 * Base plan around `now`. `anchors` pins the job running at `now` on a press (mold-in time),
 * e.g. CURE-07's medium tire molded in at 16:14 the day before (page 11: 10 h into a 28 h cure).
 */
export function baseJobs(now: number, anchors: Record<string, { moldIn: number; orderId: string; moldId: string }> = {}, nextOrder: Record<string, { orderId: string; moldId: string }> = {}): ScheduleJob[] {
  const jobs: ScheduleJob[] = []
  const counters: Record<TireSize, number> = { S: 40, M: 10, L: 6 }
  CURE_PRESS_IDS.forEach((press, i) => {
    const size = pressSize(press)
    const cure = CURE_HOURS[size]
    const anchor = anchors[press]
    // level-load: stagger presses so de-molds spread over the staffed day
    const moldIn0 = anchor?.moldIn ?? now - ((i * 0.37) % 1) * cure * HOUR - 2 * HOUR
    // two jobs back, unaligned
    let t = moldIn0 - 2 * (cure + 0.5) * HOUR
    for (let k = 0; k < 2; k++) {
      const n = counters[size]++
      jobs.push(makeJob(press, size, t, cure, `#${SIZE_PREFIX[size]}${String(n).padStart(2, '0')}`, `${size}-${press.slice(5)}`, false))
      t += (cure + 0.5) * HOUR
    }
    // the running job, then forward with staffed-window de-molds
    t = moldIn0
    let first = true
    let second = false
    while (t < now + 6 * 24 * HOUR) {
      let orderId: string
      let moldId: string
      if (first && anchor) { orderId = anchor.orderId; moldId = anchor.moldId }
      else if (second && nextOrder[press]) { orderId = nextOrder[press].orderId; moldId = nextOrder[press].moldId }
      else { const n = counters[size]++; orderId = `#${SIZE_PREFIX[size]}${String(n).padStart(2, '0')}`; moldId = `${size}-${press.slice(5)}` }
      const job = makeJob(press, size, t, cure, orderId, moldId, !first)
      jobs.push(job)
      t = Date.parse(job.demold) + 30 * MIN
      second = first
      first = false
    }
  })
  return jobs
}

export function makeSchedule(id: string, created: number, jobs: ScheduleJob[], staffing: StaffingChange[] = [], warnings: string[] = []): Schedule {
  const starts = jobs.map((j) => Date.parse(j.mold_in))
  const ends = jobs.map((j) => Date.parse(j.demold))
  return {
    id, created_at: isoTaipei(created),
    horizon_start: isoTaipei(Math.min(...starts)), horizon_end: isoTaipei(Math.max(...ends)),
    jobs, staffing_changes: staffing, solver: 'cp-sat', objective: null, warnings,
  }
}

export function version(v: number, parent: number | null, status: ScheduleVersion['status'], created: number, reason: string, eventId: string | null, schedule: Schedule): ScheduleVersion {
  return { version: v, parent_version: parent, status, created_at: isoTaipei(created), reason, event_id: eventId, schedule }
}

/** Re-flow a press's jobs from index `from` so each mold-in follows the previous de-mold + 30 min. */
export function reflow(jobs: ScheduleJob[], press: string, fromT: number): ScheduleJob[] {
  const mine = jobs.filter((j) => j.press_id === press).sort((a, b) => Date.parse(a.mold_in) - Date.parse(b.mold_in))
  const others = jobs.filter((j) => j.press_id !== press)
  const out: ScheduleJob[] = []
  let t = fromT
  for (const j of mine) {
    const mi = Date.parse(j.mold_in)
    if (mi < fromT - 1) { out.push(j); continue }
    const start = Math.max(mi, t)
    const nj = makeJob(press, j.size, start, j.cure_h, j.order_id, j.mold_id ?? '', true)
    nj.id = j.id
    out.push(nj)
    t = Date.parse(nj.demold) + 30 * MIN
  }
  return [...others, ...out]
}

/** Shift instances for days around `now` built from a ShiftDef table. */
export function shiftInstances(now: number, defs = OPTIMISED_SHIFTS, required: Record<string, number> = {}): Shift[] {
  const out: Shift[] = []
  const mid = factoryMidnight(now)
  for (let d = -2; d <= 6; d++) {
    const day = mid + d * 24 * HOUR
    const wd = weekday(day + 12 * HOUR)
    if (wd === 6) continue
    defs.forEach((s, i) => {
      const start = day + s.start * HOUR
      const end = day + (s.end > s.start ? s.end : s.end + 24) * HOUR
      const date = isoTaipei(day).slice(0, 10)
      const key = `${date}|${s.team}|${s.name}`
      out.push({
        id: `SH-${date}-${s.team}-${i}`, team: s.team, name: s.name,
        start: isoTaipei(start), end: isoTaipei(end), headcount: s.headcount,
        required: required[key] ?? s.headcount,
      })
    })
  }
  return out
}
