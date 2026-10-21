// 人力排程 (Kyle 10-22 approved draft .design/drafts/schedule/v2-simple.html): the numbers behind the one-sentence answer,
// from GET /api/shifts, the schedule in force and the waiting approvals. Display only; nothing here decides anything.
import type { ApprovalRequest, ScheduleJob, ScheduleVersion, Shift, StaffingChange } from '../../contracts'
import { hourOfDay, ms } from '../../format'

export const H = 3_600_000

/** A moment someone must be at a press: a de-mold (take the tyre out) or a mold-in (put the next one in). */
export interface Need { t: number; kind: 'demold' | 'mold_in'; job: ScheduleJob }

/** Today's shifts = the ones that START on today's date (06:00 早班 … 20:00 夜班 to 05:00), so last night's 夜班 is
 *  never drawn next to tonight's (the old page showed 胎體／胎面膠組 夜班 twice, one short and one not). */
export function todayShifts(shifts: Shift[], day: number): Shift[] {
  return shifts.filter((s) => { const a = ms(s.start); return a >= day && a < day + 24 * H })
}
export const gapOf = (s: Shift) => Math.max(0, (s.required ?? s.headcount) - s.headcount)

/** Every de-mold and mold-in of the schedule in force. A press out of service has neither after its stop (the tyre
 *  in the mold is held for QC, the next one is not put in). */
export function needsOf(jobs: ScheduleJob[], outAt: Record<string, number>): Need[] {
  const out: Need[] = []
  for (const j of jobs) {
    const stop = outAt[j.press_id]
    if (stop == null || ms(j.mold_in) < stop) out.push({ t: ms(j.mold_in), kind: 'mold_in', job: j })
    if (stop == null || ms(j.demold) < stop) out.push({ t: ms(j.demold), kind: 'demold', job: j })
  }
  return out.sort((a, b) => a.t - b.t)
}
export const isNight = (t: number) => { const h = hourOfDay(t); return h >= 20 || h < 5 }

/** 「今晚」 for a shift still to come that starts in the evening, 「今天」 for a day shift to come, 「現在」 once it runs. */
export function whenOf(s: Shift, now: number): string {
  const a = ms(s.start)
  return a <= now ? '現在' : hourOfDay(a) >= 18 ? '今晚' : '今天'
}

/** The waiting approval that decides who covers a short shift (夜班加班／從 2 線調 1 人) and its recommended option.
 *  Real backend: option args.kind overtime / staff_move; mock: args.staffing; last, the option's own words. */
export function staffingApproval(pending: ApprovalRequest[]): { req: ApprovalRequest; label: string } | null {
  for (const a of [...pending].sort((p, q) => ms(p.created_at) - ms(q.created_at))) {
    const opts = a.options.filter((o) => /^(overtime|staff_move|transfer)$/.test(String(o.args?.kind ?? o.args?.staffing ?? ''))
      || /加班|調\s*\d*\s*人/.test(o.label))
    if (opts.length) return { req: a, label: (opts.find((o) => o.recommended) ?? opts[0]).label }
  }
  return null
}

const sameShift = (c: StaffingChange, s: Shift) => c.to_team === s.team && c.shift_name === s.name && c.shift_date === s.start.slice(0, 10)

/** The newest schedule version waiting approval that covers this shift (「第 3 版排程」 in the suggestion). */
export function proposalFor(versions: ScheduleVersion[], s: Shift): { v: ScheduleVersion; c: StaffingChange } | null {
  for (const v of [...versions].filter((x) => x.status === 'proposed').sort((a, b) => b.version - a.version)) {
    const c = v.schedule.staffing_changes.find((x) => sameShift(x, s))
    if (c) return { v, c }
  }
  return null
}
