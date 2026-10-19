// The item selected in the home waiting list, shared by the home screen (list + plan) and the floating Agent drawer
// (whose conversation follows the selected item). `picked` is an event id or a home item key (order:<id>, fyi:<id>).
import { useMemo } from 'react'
import { ms } from '../format'
import { primaryEvent } from '../store'
import { useWarRoom } from '../useWarRoom'
import { homeItems } from './homeItems'

export function useHomeSelection(picked: string | null) {
  const { state } = useWarRoom()
  const events = useMemo(() => [...(state.snapshot?.events ?? [])].sort((a, b) => (a.opened_at < b.opened_at ? 1 : -1)), [state.snapshot?.events])
  const now = ms(state.clock?.factory_time ?? state.snapshot?.header.factory_time)
  const list = useMemo(() => homeItems(state.snapshot, state.pending, state.schedule, state.world, now),
    [state.snapshot, state.pending, state.schedule, state.world, now])
  const sel = list.find((i) => i.key === picked) ?? (picked ? list.find((i) => i.eventId === picked) : undefined) ?? list.find((i) => i.rank < 5) ?? null
  const ev = (sel?.eventId ? events.find((e) => e.id === sel.eventId) : null) ?? (sel ? null : primaryEvent(events))
  return { events, list, sel, ev, now }
}
