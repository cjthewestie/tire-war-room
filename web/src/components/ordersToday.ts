// Shapes of GET /api/orders/today (issue #95; built by warroom/tools/orders_today.py). Numbers here are the
// server's: a field that is null is unknown ("待確認"), never 0.
export interface OrderBrief {
  id: string; size: string; size_label: string; customer: string | null; source: 'seed' | 'world'; rush: boolean
  qty: number; done: number; remaining: number; status: string; state_text: string
  machines: string[]; machines_now: string[]; has_schedule: boolean; schedule_state: string | null
  in_press: number; scheduled_ahead: number; unscheduled: number | null
  next_demold: string | null; eta: string | null; eta_basis: string | null; eta_unknown_reason: string | null
  schedule_estimate: string | null; schedule_note: string | null
  due: string; due_at: string | null; due_basis: 'time' | 'date'
  overdue: boolean; late_by_h: number | null
  risk: string; risk_text: string; reasons: string[]
  in_production_today: boolean; production_basis: string[]; due_today: boolean
}
export interface OrdersToday {
  date: string; as_of: string; schedule_version: number | null
  orders: OrderBrief[]; duplicates: string[]
  production: string[]; delivery: string[]; overdue: string[]; late: string[]; other_risk: string[]; at_risk: string[]
  counts: { total: number; production: number; delivery: number; overdue: number; late: number; other_risk: number; at_risk: number; unique_today: number }
}

/** One id per page load; the server keeps order ids only, keyed by it, so a reload starts a new conversation. */
export const newConversationId = () => `c-${Math.random().toString(36).slice(2, 10)}${Date.now().toString(36)}`
