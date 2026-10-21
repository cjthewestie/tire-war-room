// 未來 7 日交貨計畫: seven calendar days starting at the simulation's factory day, each card lists the orders whose
// promised due date falls on that day (counted once per order, by due date only -- never by ETA, created_at or the
// computer's own clock). Cards are one fixed height; at most two orders are named, the rest is 「另有 X 筆」 and the
// full list is the 全部訂單 table below, filtered by clicking the day (OrdersTab owns the filter state).
// Risk comes from orderModel's existing risk level; an unscheduled order is not treated as late here.
import { useState } from 'react'
import { SIZE_LABEL } from '../factory'
import { fmtMD, fmtWeekday } from '../format'
import type { OrderView } from './orderModel'
import { dueDays, isRisk } from './dueWeek'

const SHOWN = 2

export function DueWeekPlan({ views, now, day, onPick }: { views: OrderView[]; now: number; day: string | null; onPick: (key: string | null) => void }) {
  const [open, setOpen] = useState(true)
  const days = dueDays(views, now)
  return (
    <section className="panel due-week" data-testid="due-week" aria-label="未來 7 日交貨計畫">
      <header className="dw-h">
        <div>
          <h2>未來 7 日交貨計畫</h2>
          <span className="muted small">以交貨日期為主，點日期可篩選下方訂單</span>
        </div>
        <button type="button" className="dw-fold" onClick={() => setOpen(!open)} aria-expanded={open} aria-label={open ? '收合交貨計畫' : '展開交貨計畫'}>{open ? '︿' : '﹀'}</button>
      </header>
      {open && (days.length ? (
        <ul className="dw-days" role="list">
          {days.map((d) => (
            <li key={d.key} className="dw-li" data-day={d.key}>
              <button type="button" className={`dw-day ${d.today ? 'is-today' : ''} ${d.risk ? 'has-risk' : ''} ${day === d.key ? 'on' : ''}`}
                aria-pressed={day === d.key} onClick={() => onPick(day === d.key ? null : d.key)}
                aria-label={`${fmtMD(d.t)} ${d.orders.length ? `${d.orders.length} 張應交貨${d.risk ? `，${d.risk} 張有風險` : ''}` : '無交貨'}，點擊篩選訂單`}>
                <span className="dw-date"><b>{fmtMD(d.t)}</b>（{fmtWeekday(d.t).replace('週', '')}）{d.today && <em className="dw-today">今天</em>}</span>
                <span className="dw-n"><b>{d.orders.length}</b> 張</span>
                <span className={`dw-sub ${d.risk ? 'risk' : ''}`}>{d.orders.length ? (d.risk ? `${d.risk} 張有風險` : ' ') : '無交貨'}</span>
                <span className="dw-orders">
                  {d.orders.slice(0, SHOWN).map((v) => (
                    <span key={v.o.id} className={`dw-o ${isRisk(v) ? 'is-risk' : ''}`} data-order={v.o.id}>
                      <b>{v.o.id}</b>
                      <small>{v.risk === 'done' ? '已出貨・' : v.risk === 'complete' ? '已完成・' : ''}{SIZE_LABEL[v.o.size]} {v.o.qty} 條</small>
                    </span>
                  ))}
                  {d.orders.length > SHOWN && <span className="dw-more">另有 {d.orders.length - SHOWN} 筆</span>}
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : <p className="muted small">讀取工廠時間中…</p>)}
    </section>
  )
}
