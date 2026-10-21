// GET /api/health, read once per page and shared (AiStatusLine's agent_runtime, the 3D ring's recover_fast_s). Null in
// the in-browser mock (no server) or while it loads.
import { useEffect, useState } from 'react'
import { API, type HealthStatus } from './contracts'

let health: Promise<HealthStatus | null> | null = null

export function useHealth(): HealthStatus | null {
  const [v, setV] = useState<HealthStatus | null>(null)
  useEffect(() => {
    health ??= fetch(API.health).then((r) => r.json() as Promise<HealthStatus>).then((h) => h, () => null)
    let alive = true
    health.then((x) => { if (alive) setV(x) }, () => {})
    return () => { alive = false }
  }, [])
  return v
}

/** RECOVER_FAST_S when the server says (the contract's default otherwise): wall seconds the readings take back to normal. */
export const RECOVER_FAST_S_DEFAULT = 4
