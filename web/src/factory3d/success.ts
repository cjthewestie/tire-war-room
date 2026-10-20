// Success moments (issue #36, CJ 2026-10-20 「成功出貨或者是單子完成也要顯示」): the world feed's order_completed and
// shipment_departed events, each shown once. Events already in the feed when the page opens are not replayed.
import { useEffect, useRef, useState } from 'react'
import type { WorldEvent, WorldSnapshot } from '../contracts'

export interface SuccessMoment { key: string; kind: 'order_completed' | 'shipment_departed'; text: string; orderIds: string[]; qty: number | null; onTime: boolean | null; at: number }

const KINDS = new Set(['order_completed', 'shipment_departed'])
const keyOf = (e: WorldEvent) => `${e.ts}|${e.kind}|${(e.order_ids ?? []).join(',')}`

export function successTitle(m: SuccessMoment): string {
  const id = m.orderIds.join('、') || '—'
  const n = m.qty != null ? `（${m.qty} 條）` : ''
  const late = m.onTime === false ? '・逾期' : ''
  return m.kind === 'order_completed' ? `✅ 訂單 ${id} 完成${n}${late}` : `🚚 出貨 ${id} 已出廠${n}${late}`
}

/** The newest success moment (null until one happens after the page opened). */
export function useSuccessMoments(world: WorldSnapshot | null): { latest: SuccessMoment | null; all: SuccessMoment[] } {
  const seen = useRef<Set<string> | null>(null)
  const [all, setAll] = useState<SuccessMoment[]>([])
  useEffect(() => {
    if (!world) return
    const evs = world.events.filter((e) => KINDS.has(e.kind))
    if (seen.current === null) { seen.current = new Set(evs.map(keyOf)); return }
    const fresh = evs.filter((e) => !seen.current!.has(keyOf(e)))
    if (!fresh.length) return
    for (const e of fresh) seen.current.add(keyOf(e))
    const now = Date.now()
    setAll((xs) => [...xs, ...fresh.map((e, i) => ({
      key: keyOf(e), kind: e.kind as SuccessMoment['kind'], text: e.text, orderIds: e.order_ids ?? [], qty: e.qty ?? null,
      onTime: e.on_time ?? null, at: now + i,
    }))].slice(-20))
  }, [world])
  return { latest: all.length ? all[all.length - 1] : null, all }
}
