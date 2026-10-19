// Goods status (issue #17): every planned tyre with a status ring — 待補料 → 備料中 → 生產中 → 硫化中 → 完成 → 待出貨 → 已出貨.
// Order as the backend sends it (in presses, then the queue by mold-in, then de-molded), so a rush order visibly
// moves up the queue after its schedule is approved.
import type { GoodsItem, GoodsView, OrderGoods } from '../contracts'
import { GOODS_ORDER, GOODS_TONE, movedText } from './goods'
import { ProgressRing } from './ProgressRing'
import './work.css'

export function GoodsCounts({ goods }: { goods: GoodsView }) {
  return (
    <ol className="pipe goods-pipe" data-testid="goods-counts">
      {GOODS_ORDER.map((s) => <li key={s} className={goods.counts[s] ? 'has' : ''}><b>{goods.counts[s] ?? 0}</b><span>{s}</span></li>)}
    </ol>
  )
}

export function GoodsQueue({ goods, limit = 12 }: { goods: GoodsView; limit?: number }) {
  const moved = new Map(goods.orders.map((o) => [o.order_id, o]))
  // two parts, each in the backend's order: the queue (by mold-in: a rush order shows where it was inserted), then
  // the tyres in the presses and the finished ones
  const queue = goods.items.filter((g) => g.status === '待補料' || g.status === '備料中' || g.status === '生產中')
  const rest = goods.items.filter((g) => !queue.includes(g))
  const part = (items: GoodsItem[], title: string, testId: string) => (
    <>
      <li className="muted small goods-h">{title}（{items.length}）</li>
      {items.slice(0, limit).map((g) => <GoodsRow key={g.id} g={g} order={moved.get(g.order_id)} />)}
      {items.length > limit && <li className="muted small" data-testid={testId}>另 {items.length - limit} 條</li>}
    </>
  )
  return (
    <ul className="goods" data-testid="goods-queue">
      {part(queue, '佇列（依入模時間）', 'goods-more-queue')}
      {part(rest, '硫化中・完成', 'goods-more-rest')}
    </ul>
  )
}

function GoodsRow({ g, order }: { g: GoodsItem; order?: OrderGoods }) {
  const mv = movedText(order?.moved_h)
  return (
    <li className={`good st-${g.status}${g.blocked ? ' blocked' : ''}${g.late ? ' late' : ''}`} data-testid="goods-item"
      data-status={g.status} data-order={g.order_id} data-id={g.id}>
      <ProgressRing value={g.progress} tone={g.late ? 'alarm' : GOODS_TONE[g.status]} size={24} title={`${g.status} ${Math.round(g.progress * 100)}%`} />
      <span className="good-s">{g.status}</span>
      <b>{g.order_id}</b>{g.rush && <i className="tag rush">急單</i>}
      <span className="muted small good-n">{g.note ?? ''}{g.blocked && !g.note?.includes('補料') ? '・等補料' : ''}{g.late ? '・已逾備料時間' : ''}</span>
      {mv && <i className={`tag moved ${order && order.moved_h! > 0 ? 'later' : 'earlier'}`}>{mv}</i>}
    </li>
  )
}
