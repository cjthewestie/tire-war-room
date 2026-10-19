// 訂單 (UX brief moment 「查一張單」): one search box answers "where is it, will it ship on time, what blocks it".
// Data: GET /api/world orders (qty, done, due, status) + the active schedule's jobs for that order (A 線投料, 貼合,
// 入模, 出模 times). Stage progress counts the order's scheduled tires whose step time has passed the factory clock.
import { useMemo, useState } from 'react'
import { SIZE_LABEL } from '../factory'
import { fmtMD, ms } from '../format'
import { useWarRoom } from '../useWarRoom'
import { Panel } from './common'
import { OrderCard } from './OrderCard'

export function OrdersTab() {
  const { state } = useWarRoom()
  const orders = useMemo(() => state.world?.orders.items ?? [], [state.world?.orders.items])
  const jobs = state.schedule?.schedule.jobs ?? []
  const now = ms(state.clock?.factory_time ?? state.snapshot?.header.factory_time)
  const [q, setQ] = useState('')
  const [picked, setPicked] = useState<string | null>(null)
  const query = q.trim().toLowerCase().replace(/^#/, '')
  const matches = query ? orders.filter((o) => `${o.id} ${o.customer}`.toLowerCase().replace(/#/g, '').includes(query)) : orders
  const def = orders.find((o) => o.rush && o.status !== '已出貨') ?? orders.find((o) => o.status === '生產中') ?? orders[0]
  const order = orders.find((o) => o.id === picked) ?? (query ? matches[0] : def) ?? null
  const warnings = state.schedule?.schedule.warnings ?? []
  if (!state.world) return <div className="loading">讀取訂單中…</div>
  return (
    <div className="orders-tab" data-testid="orders-tab">
      <form className="order-search" role="search" onSubmit={(e) => { e.preventDefault(); if (matches[0]) setPicked(matches[0].id) }}>
        <input value={q} onChange={(e) => { setQ(e.target.value); setPicked(null) }} placeholder="輸入訂單號或客戶，例如 A-311" aria-label="查訂單" data-testid="order-search" />
        <button type="submit" className="btn-approve">查</button>
      </form>
      {query && !matches.length && <p className="note">找不到「{q}」。請確認訂單號，或清空搜尋看全部訂單。</p>}
      {order && <OrderCard o={order} jobs={jobs.filter((j) => j.order_id === order.id)} now={now} warnings={warnings.filter((w) => w.includes(order.id))} />}
      <Panel title={query ? `符合的訂單（${matches.length}）` : `全部訂單（${orders.length}）`} className="order-list-panel">
        <div className="order-list" role="list">
          {matches.map((o) => (
            <button key={o.id} role="listitem" className={`order-li ${order?.id === o.id ? 'on' : ''}`} onClick={() => setPicked(o.id)}>
              <b>{o.id}</b>{o.rush && <em className="tag rush">急單</em>}<span className="muted">{o.customer}・{SIZE_LABEL[o.size]}</span>
              <span className="order-li-r">{o.done}/{o.qty}・{o.status}・交期 {fmtMD(o.due)}</span>
            </button>
          ))}
        </div>
      </Panel>
    </div>
  )
}
