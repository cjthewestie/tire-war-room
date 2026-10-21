// The 公版 AI 處理狀態 line (logic in aiStatus.ts).
import type { Event } from './contracts'
import { aiStatusParts, repairStage } from './aiStatus'
import { useHealth } from './health'
import { fastApplies, fastRepairOf } from './phone/fastEta'
import { useWarRoom } from './useWarRoom'

/** The 公版 line. `className` lets each screen keep its own box (3D HUD pill, phone card row, drawer note).
 *  While the card waits, with fast repair, the time is 「核准後約 12 秒維修完成」 (aiStatusParts' eta) instead of the
 *  verification window (「約 30 分鐘」), on every screen: the phone card and the 3D answer card side by side gave two times.
 *  `compact` (3D demo HUD only, under the answer card's headline that already says what happened): just what Agent Nemo
 *  does and when, from the same parts as the full line: a card recomputed while the crew already repairs (concurrent_3:
 *  3 號's stale schedule) says 已派人維修 and the repair's seconds, never 「核准後約 12 秒」 again. */
export function AiStatusLine({ ev, className, compact = false }: { ev: Event | null; className?: string; compact?: boolean }) {
  const { state } = useWarRoom()
  const health = useHealth()
  if (!ev) return null
  const p = aiStatusParts(ev, state, health?.agent_runtime ?? null, health?.repair_fast_s ?? null)
  const fast = fastRepairOf(health)
  const items = state.world?.tasks.items ?? []
  const stage = repairStage(ev, items)
  // the card waits and nobody has been sent yet (a recomputed card while the crew repairs is not this)
  const fixes = ev.state === 'await_approval' && stage == null && fastApplies(ev, items.filter((t) => t.event_id === ev.id))
  const fixBy = fixes && fast ? `核准後約 ${Math.max(1, Math.round(fast.repair_s))} 秒維修完成` : ''   // the same words as aiStatusParts' eta (veteran 10-22)
  if (compact) {
    // with fast repair the verification window (「約 30 分鐘」) is not the time: no time until the crew is on it; once it
    // is, the repair's own seconds (also while code re-solves the approved round's schedule)
    const when = fixBy || (fast && stage == null && !['execute', 'verify', 'closed'].includes(ev.state) ? '' : p.eta)
    // under the headline: Agent Nemo by name, no task count, and not 「已恢復正常」 a second time
    const phase = p.phase.replace(/^AI /, 'Agent Nemo ').replace(/（\d+\/\d+）$/, '').replace(/^已恢復正常・報告已產生$/, '報告在手機上')
    // the headline already says 維修中: under it who Agent Nemo sent (veteran glossary 派人維修)
    const what = fixes ? (p.who || 'Agent Nemo 建議派人維修') : stage === 'repairing' && p.who ? p.who : phase
    return (
      <p className={`ai-status compact ${className ?? ''}`} data-testid="ai-status">
        <span>{what}</span>
        {when && <span>{when}</span>}
      </p>
    )
  }
  return (
    <p className={`ai-status ${className ?? ''}`} data-testid="ai-status">
      <b>{p.lead}</b>
      {p.who && <span>{p.who}</span>}
      {p.phase && <span>{p.phase}</span>}
      {p.eta && <span>{p.eta}</span>}
    </p>
  )
}
