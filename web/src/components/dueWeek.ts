// Pure helpers for 未來 7 日交貨計畫 (DueWeekPlan.tsx): which factory day an order is due on, and the 7 day window.
import { factoryMidnight, isoTaipei, ms } from '../format'
import type { OrderView } from './orderModel'

const DAY = 24 * 3_600_000

/** Factory-day key (YYYY-MM-DD, +08:00) of an order's promised due: a date stays as is, a timestamp is read in factory time. */
export function dueKey(due: string): string {
  if (/^\d{4}-\d{2}-\d{2}$/.test(due)) return due
  const t = ms(due)
  return isFinite(t) ? isoTaipei(t).slice(0, 10) : ''
}
export const isRisk = (v: OrderView) => v.risk === 'late' || v.risk === 'risk'

export interface DueDay { key: string; t: number; today: boolean; orders: OrderView[]; risk: number }

/** Today + the next 6 factory days; `now` is the simulation clock (epoch ms). Empty until the clock is known. */
export function dueDays(views: OrderView[], now: number): DueDay[] {
  if (!isFinite(now)) return []
  const start = factoryMidnight(now)
  return Array.from({ length: 7 }, (_, i) => {
    const t = start + i * DAY
    const key = isoTaipei(t).slice(0, 10)
    const orders = views.filter((v) => dueKey(v.o.due) === key)
    return { key, t, today: i === 0, orders, risk: orders.filter(isRisk).length }
  })
}

