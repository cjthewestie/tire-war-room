// GET /api/health for screens that need a server setting (快速維修 REPAIR_FAST_S / RECOVER_FAST_S on the phone's 修復預估):
// one request shared by every caller on the page (cached 15 s), refreshed every 20 s while live; null in the in-browser
// mock, before the first answer, or when the server does not answer.
import { useEffect, useState } from 'react'
import { API, type HealthStatus } from './contracts'
import { apiGet } from './transport'
import { useWarRoom } from './useWarRoom'

/** recover_fast_s: RECOVER_FAST_S (wall seconds the readings take to ramp back after a fast repair); older servers lack it. */
export type Health = HealthStatus & { recover_fast_s?: number }

const MAX_AGE_MS = 15_000
let last: Health | null = null
let cache: { at: number; p: Promise<Health | null> } | null = null

function loadHealth(): Promise<Health | null> {
  if (!cache || Date.now() - cache.at > MAX_AGE_MS) {
    cache = { at: Date.now(), p: apiGet<Health>(API.health).then((h) => (last = h), () => null) }
  }
  return cache.p
}

export function useHealth(): Health | null {
  const { transport, connected } = useWarRoom()
  const live = transport?.mode === 'live'
  const [health, setHealth] = useState<Health | null>(last)
  useEffect(() => {
    if (!live || !connected) return
    let off = false
    const load = () => loadHealth().then((h) => { if (!off && h) setHealth(h) })
    load()
    const t = setInterval(load, 20_000)
    return () => { off = true; clearInterval(t) }
  }, [live, connected])
  return live ? health : null
}
