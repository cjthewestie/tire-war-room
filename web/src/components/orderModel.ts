// One order's state, computed once for the 訂單 list and the order card: per-stage progress (投料 → 貼合 →
// 入模硫化 → 出模 → 完成 → 出貨) from the active schedule's jobs against the factory clock, the ETA (last
// scheduled de-mold) against the due date, and a risk level so at-risk orders sort first.
import type { OrderGoods, ScheduleJob, WorldOrder } from '../contracts'
import { ms } from '../format'

const H = 3_600_000

/** The order pipeline as GET /api/world counts it. */
export const ORDER_STAGES = ['待排程', '生產中', '完成', '待出貨', '已出貨'] as const

export function dueEnd(due: string): number {
  // due is a date (YYYY-MM-DD) or a timestamp; a date means the end of that factory day
  return /^\d{4}-\d{2}-\d{2}$/.test(due) ? ms(`${due}T23:59:00+08:00`) : ms(due)
}

export type Risk = 'late' | 'risk' | 'ok' | 'done'
export const RISK_TEXT: Record<Risk, string> = { late: '會晚交', risk: '有風險', ok: '準時', done: '已出貨' }

export interface OrderView {
  o: WorldOrder
  jobs: ScheduleJob[]                         // this order's scheduled tyres, by mold-in
  stages: { k: string; v: number; of: number }[]
  eta: number                                 // last scheduled de-mold (NaN: none in the schedule window)
  due: number
  lateH: number | null                        // hours past due (eta − due), null when on time or unknown
  risk: Risk
  why: string[]                               // plain reasons for the risk
  unscheduled: number                         // tyres still to make that the schedule window does not hold
  projected: string | null                    // "10/16": the schedule's capacity estimate past its window (warnings)
}

/** The part of the schedule's warnings about one order: a warning lists several orders
 *  (「本週排程外，依產能預估晚於交期：#S-102 剩 20 顆預估 10/16（交期 10/15）、#M-201 …」). */
export function warningsFor(id: string, warnings: string[]): string[] {
  const out: string[] = []
  for (const w of warnings) {
    if (!w.includes(id)) continue
    const part = w.split(/[：、；]/).find((x) => x.includes(id))
    out.push((part ?? w).trim())
  }
  return out
}

export function orderView(o: WorldOrder, allJobs: ScheduleJob[], now: number, warnings: string[], goods?: OrderGoods): OrderView {
  const jobs = allJobs.filter((j) => j.order_id === o.id).sort((a, b) => ms(a.mold_in) - ms(b.mold_in))
  const passed = (t: string) => isFinite(now) && ms(t) <= now
  const n = jobs.length
  const shipped = o.status === '已出貨'
  const stages = [
    { k: '投料', v: jobs.filter((j) => passed(j.a_feed_start)).length, of: n },
    { k: '貼合', v: jobs.filter((j) => passed(j.bond_start)).length, of: n },
    { k: '入模硫化', v: jobs.filter((j) => passed(j.mold_in)).length, of: n },
    { k: '出模', v: jobs.filter((j) => passed(j.demold)).length, of: n },
    { k: '完成', v: o.done, of: o.qty },
    { k: '出貨', v: shipped ? o.qty : 0, of: o.qty },
  ]
  const eta = n ? Math.max(...jobs.map((j) => ms(j.demold))) : NaN
  const due = dueEnd(o.due)
  const late = isFinite(eta) && isFinite(due) && eta > due
  const mine = warningsFor(o.id, warnings)
  const projected = mine.map((w) => /預估\s*(\d{1,2}\/\d{1,2})/.exec(w)?.[1]).find(Boolean) ?? null
  const left = o.qty - o.done
  const unscheduled = shipped || o.status === '完成' || o.status === '待出貨' ? 0 : Math.max(0, left - jobs.filter((j) => !passed(j.demold)).length)
  const why: string[] = []
  if (late) why.push(`最後一條出模比交期晚 ${Math.ceil((eta - due) / H)} h`)
  why.push(...mine.map((w) => `排程視窗外預估：${w.replace(o.id, '').trim()}`))
  if (goods?.late && !late) why.push('貨品狀態預估會誤交期')
  if (!n && !shipped && o.status === '待排程') why.push('還沒排進硫化排程')
  else if (unscheduled > 0 && isFinite(due) && due - now < 72 * H) why.push(`還有 ${unscheduled} 條不在排程視窗內，交期 3 天內`)
  // the schedule's own capacity estimate past its window ("預估 10/16"), compared with the due day
  const pm = projected ? /^(\d{1,2})\/(\d{1,2})$/.exec(projected) : null
  const projLate = !!pm && isFinite(due) && ms(`${o.due.slice(0, 4)}-${pm[1].padStart(2, '0')}-${pm[2].padStart(2, '0')}T23:59:00+08:00`) > due
  const risk: Risk = shipped ? 'done' : late || projLate || goods?.late ? 'late' : why.length ? 'risk' : 'ok'
  return { o, jobs, stages, eta, due, lateH: late ? Math.ceil((eta - due) / H) : null, risk, why, unscheduled, projected }
}

const RANK: Record<Risk, number> = { late: 0, risk: 1, ok: 2, done: 3 }

/** At-risk first, then rush, then the nearest due date. */
export function byRisk(a: OrderView, b: OrderView): number {
  return RANK[a.risk] - RANK[b.risk] || Number(b.o.rush) - Number(a.o.rush) || a.due - b.due || (a.o.id < b.o.id ? -1 : 1)
}
