// Success moments (issue #36, CJ 2026-10-20 「成功出貨或者是單子完成也要顯示」): the world feed's order_completed and
// shipment_departed events, each shown once. Events already in the feed when the page opens are not replayed.
// Demo (?demo=1): a repaired machine back to normal (Event.recovered_at, Kyle 10-21 flow) is one too, 「7 號硫化機 已恢復正常」,
// the positive end of the anomaly story; the same rule, a machine already recovered when the page opens is not replayed.
import { useEffect, useRef, useState } from 'react'
import type { Event, WorldEvent, WorldSnapshot } from '../contracts'
import { machineLabel } from '../factory'
import { recoveryPhase } from './machineRing'

export interface SuccessMoment { key: string; kind: 'order_completed' | 'shipment_departed' | 'recovered'; text: string; orderIds: string[]; qty: number | null; onTime: boolean | null; at: number }

const KINDS = new Set(['order_completed', 'shipment_departed'])
const keyOf = (e: WorldEvent) => `${e.ts}|${e.kind}|${(e.order_ids ?? []).join(',')}`
const backKey = (e: Event) => `${e.id}|recovered`

export function successTitle(m: SuccessMoment): string {
  if (m.kind === 'recovered') return `${m.text} 已恢復正常`
  const id = m.orderIds.join('、') || '—'
  const n = m.qty != null ? `（${m.qty} 條）` : ''
  const late = m.onTime === false ? '・逾期' : ''
  return m.kind === 'order_completed' ? `✅ 訂單 ${id} 完成${n}${late}` : `🚚 出貨 ${id} 已出廠${n}${late}`
}

/** The newest success moment (null until one happens after the page opened). `events`: the snapshot's events, for the
 *  已恢復正常 moment (left out: none). */
export function useSuccessMoments(world: WorldSnapshot | null, events?: Event[]): { latest: SuccessMoment | null; all: SuccessMoment[] } {
  const seen = useRef<Set<string> | null>(null)
  const back = useRef<Set<string> | null>(null)
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
  useEffect(() => {
    if (!events) return
    const done = events.filter((e) => recoveryPhase(e) === 'recovered')
    if (back.current === null) { back.current = new Set(done.map(backKey)); return }
    const fresh = done.filter((e) => !back.current!.has(backKey(e)))
    if (!fresh.length) return
    for (const e of fresh) back.current.add(backKey(e))
    const now = Date.now()
    setAll((xs) => [...xs, ...fresh.map((e, i) => ({
      key: backKey(e), kind: 'recovered' as const, text: machineLabel(e.machine_id), orderIds: [], qty: null, onTime: null, at: now + i,
    }))].slice(-20))
  }, [events])
  return { latest: all.length ? all[all.length - 1] : null, all }
}
