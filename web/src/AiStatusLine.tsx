// The 公版 AI 處理狀態 line (logic in aiStatus.ts).
import type { Event } from './contracts'
import { aiStatusParts } from './aiStatus'
import { useHealth } from './health'
import { useWarRoom } from './useWarRoom'

/** The 公版 line. `className` lets each screen keep its own box (3D HUD pill, phone card row, drawer note). */
export function AiStatusLine({ ev, className }: { ev: Event | null; className?: string }) {
  const { state } = useWarRoom()
  const runtime = useHealth()?.agent_runtime ?? null
  if (!ev) return null
  const p = aiStatusParts(ev, state, runtime)
  return (
    <p className={`ai-status ${className ?? ''}`} data-testid="ai-status">
      <b>AI 偵測到：{p.what}</b>
      <span>{p.who}</span>
      <span>{p.phase}</span>
      {p.eta && <span>{p.eta}</span>}
    </p>
  )
}
