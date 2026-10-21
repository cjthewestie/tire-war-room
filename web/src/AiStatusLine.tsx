// The 公版 AI 處理狀態 line (logic in aiStatus.ts).
import type { Event } from './contracts'
import { aiStatusParts } from './aiStatus'
import { useHealth } from './health'
import { fastApplies, fastRepairOf } from './phone/fastEta'
import { useWarRoom } from './useWarRoom'

/** The 公版 line. `className` lets each screen keep its own box (3D HUD pill, phone card row, drawer note).
 *  `compact` (3D demo HUD only, under the answer card's headline that already says what happened): just what Agent Nemo
 *  does and when; while the card waits, with fast repair, the whole wall-clock time 「核准後約 16 秒修好」 instead of
 *  the verification window. */
export function AiStatusLine({ ev, className, compact = false }: { ev: Event | null; className?: string; compact?: boolean }) {
  const { state } = useWarRoom()
  const health = useHealth()
  if (!ev) return null
  const p = aiStatusParts(ev, state, health?.agent_runtime ?? null, health?.repair_fast_s ?? null)
  if (compact) {
    const fast = fastRepairOf(health)
    const fixes = ev.state === 'await_approval' && fastApplies(ev, (state.world?.tasks.items ?? []).filter((t) => t.event_id === ev.id))
    // with fast repair the verification window (「約 30 分鐘」) is not the time: no time until the crew is on it
    const when = fixes && fast ? `核准後約 ${Math.round((fast.repair_s + fast.recover_s) * 10) / 10} 秒修好`
      : fast && !['execute', 'verify', 'closed'].includes(ev.state) ? '' : p.eta
    return (
      <p className={`ai-status compact ${className ?? ''}`} data-testid="ai-status">
        <span>{fixes ? 'Agent Nemo 會派人維修' : p.phase}</span>
        {when && <span>{when}</span>}
      </p>
    )
  }
  return (
    <p className={`ai-status ${className ?? ''}`} data-testid="ai-status">
      <b>AI 偵測到：{p.what}</b>
      <span>{p.who}</span>
      <span>{p.phase}</span>
      {p.eta && <span>{p.eta}</span>}
    </p>
  )
}
