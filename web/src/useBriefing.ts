// Shared by the phone page and the desktop Agent panel: the live store → briefing.ts input, so both draw the same words.
import { useMemo } from 'react'
import type { Event } from './contracts'
import { aiStatusParts, repairStage } from './aiStatus'
import { buildBriefing, type BriefCtx, type Briefing } from './briefing'
import { useHealth } from './health'
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

/** The newest briefing of one event: 待核准 while a card waits, then 執行中, then 處置完成 (null while still planning).
 *  With fast repair (/api/health repair_fast_s) 執行中 follows the crew: 維修中 with the 公版 line's seconds, 維修完成. */
export function useEventBriefing(ev: Event | null): Briefing | null {
  const { state } = useWarRoom()
  const ctx = useBriefCtx()
  const fastS = useHealth()?.repair_fast_s ?? null
  const req = ev ? [...state.pending].filter((p) => p.event_id === ev.id).sort((a, b) => (a.created_at < b.created_at ? 1 : -1))[0] ?? null : null
  const stage = ev && fastS && fastS > 0 ? repairStage(ev, ctx.tasks) : null
  const parts = stage === 'repairing' || stage === 'recovering' ? aiStatusParts(ev!, state, null, fastS) : null
  const phase = parts?.phase ?? '', eta = parts?.eta ?? ''
  return useMemo(() => buildBriefing({ ev, req, chosen: null, ...ctx,
    repair: (stage === 'repairing' || stage === 'recovering') ? { stage, phase, eta } : null }),
    // the store objects change identity on every refresh; their contents are what the words depend on
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [ev, req, ctx.resolved, ctx.tasks, ctx.machines, ctx.now, ctx.maintenance, stage, phase, eta])
}
