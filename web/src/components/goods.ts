// issue #17 goods-status helpers shared by GoodsQueue and WorldTab (kept out of the .tsx for fast refresh).
import type { GoodsStatus } from '../contracts'
import type { RingTone } from './ProgressRing'

export const GOODS_ORDER: GoodsStatus[] = ['待補料', '備料中', '生產中', '硫化中', '完成', '待出貨', '已出貨']
export const GOODS_TONE: Record<GoodsStatus, RingTone> = {
  待補料: 'idle', 備料中: 'warn', 生產中: 'warn', 硫化中: 'live', 完成: 'done', 待出貨: 'done', 已出貨: 'done',
}

export function movedText(h: number | null | undefined): string | null {
  if (h == null || Math.abs(h) < 0.1) return null
  return h > 0 ? `延後 +${h.toFixed(1)} h` : `提前 ${Math.abs(h).toFixed(1)} h`
}

