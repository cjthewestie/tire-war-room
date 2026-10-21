// Shared by the phone page and the desktop Agent panel: the live store → briefing.ts input, so both draw the same words.
import { useMemo } from 'react'
import type { Event } from './contracts'
import { buildBriefing, type BriefCtx, type Briefing } from './briefing'
import { useWarRoom } from './useWarRoom'

/** Everything the briefing needs besides the card itself, read from the live store. */
export function useBriefCtx(): Pick<BriefCtx, 'resolved' | 'tasks' | 'machines' | 'now' | 'maintenance'> {
  const { state } = useWarRoom()
  return {
    resolved: state.resolved,
    tasks: state.world?.tasks.items ?? [],
    machines: state.snapshot?.machines ?? [],
    now: state.clock?.factory_time ?? state.snapshot?.header.factory_time ?? null,
    maintenance: (state.world?.maintenance.in_progress ?? []).map((m) => ({ machine_id: m.machine_id, title: m.title, status: m.status })),
  }
}

/** The newest briefing of one event: 待核准 while a card waits, then 執行中, then 處置完成 (null while still planning). */
export function useEventBriefing(ev: Event | null): Briefing | null {
  const { state } = useWarRoom()
  const ctx = useBriefCtx()
  const req = ev ? [...state.pending].filter((p) => p.event_id === ev.id).sort((a, b) => (a.created_at < b.created_at ? 1 : -1))[0] ?? null : null
  return useMemo(() => buildBriefing({ ev, req, chosen: null, ...ctx }),
    // the store objects change identity on every refresh; their contents are what the words depend on
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [ev, req, ctx.resolved, ctx.tasks, ctx.machines, ctx.now, ctx.maintenance])
}
