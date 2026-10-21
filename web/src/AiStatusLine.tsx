// The 公版 AI 處理狀態 line (logic in aiStatus.ts).
import { useEffect, useState } from 'react'
import { API, type Event } from './contracts'
import { aiStatusParts } from './aiStatus'
import { useWarRoom } from './useWarRoom'

let runtimeLabel: Promise<string | null> | null = null
function useAgentRuntime(): string | null {
  const [v, setV] = useState<string | null>(null)
  useEffect(() => {
    runtimeLabel ??= fetch(API.health).then((r) => r.json()).then((h) => h.agent_runtime ?? null, () => null)
    let alive = true
    runtimeLabel.then((x) => { if (alive) setV(x) }, () => {})
    return () => { alive = false }
  }, [])
  return v
}

/** The 公版 line. `className` lets each screen keep its own box (3D HUD pill, phone card row, drawer note). */
export function AiStatusLine({ ev, className }: { ev: Event | null; className?: string }) {
  const { state } = useWarRoom()
  const runtime = useAgentRuntime()
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
