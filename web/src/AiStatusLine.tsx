// The 公版 AI 處理狀態 line (logic in aiStatus.ts).
import type { Event } from './contracts'
import { aiStatusParts } from './aiStatus'
import { useHealth } from './health'
import { useWarRoom } from './useWarRoom'

/** The 公版 line. `className` lets each screen keep its own box (3D HUD pill, phone card row, drawer note). */
export function AiStatusLine({ ev, className }: { ev: Event | null; className?: string }) {
  const { state } = useWarRoom()
  const health = useHealth()
  if (!ev) return null
  const p = aiStatusParts(ev, state, health?.agent_runtime ?? null, health?.repair_fast_s ?? null)
  return (
    <p className={`ai-status ${className ?? ''}`} data-testid="ai-status">
      <b>{p.lead}</b>
      {p.who && <span>{p.who}</span>}
      {p.phase && <span>{p.phase}</span>}
      {p.eta && <span>{p.eta}</span>}
    </p>
  )
}
