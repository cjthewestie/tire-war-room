// The wall clock behind the 3D ring's 回溫中 / 已恢復 (machineRing.ts ringRecoveries): when each repaired event's phase was
// first seen, and a 250 ms tick while a ramp or a green hold is on screen. RECOVER_FAST_S from /api/health.
import { useEffect, useMemo, useRef, useState } from 'react'
import type { Event } from '../contracts'
import { RECOVER_FAST_S_DEFAULT, useHealth } from '../health'
import { recoveryKey, recoveryPhase, ringRecoveries, type RingRecovery } from './machineRing'

const TICK_MS = 250
const TICK_FOR_MS = 10_000      // keep ticking this long after a phase was first seen (the ramp, then the green hold)

export function useRingRecoveries(events: Event[] | undefined, now: number, speed: number): Record<string, RingRecovery> {
  const recoverS = useHealth()?.recover_fast_s ?? RECOVER_FAST_S_DEFAULT
  const [clock, setClock] = useState(() => ({ wall: Date.now(), seen: {} as Record<string, number> }))
  const keys = useMemo(() => (events ?? []).flatMap((e) => { const ph = recoveryPhase(e); return ph ? [recoveryKey(e, ph)] : [] }), [events])
  const keysRef = useRef(keys)
  useEffect(() => { keysRef.current = keys }, [keys])
  // a phase not recorded yet counts as seen now (ringRecoveries); the next tick records it. A ramp ticks for its whole
  // length too (RECOVER_FAST_S may be over TICK_FOR_MS: the ring would stop filling half way)
  const live = keys.some((k) => clock.seen[k] == null || clock.wall - clock.seen[k] < TICK_FOR_MS + (k.endsWith('|recovering') ? recoverS * 1000 : 0))
  useEffect(() => {
    if (!live) return
    const t = window.setInterval(() => setClock((c) => {
      const wall = Date.now()
      const fresh = keysRef.current.filter((k) => c.seen[k] == null)
      return { wall, seen: fresh.length ? { ...c.seen, ...Object.fromEntries(fresh.map((k) => [k, wall])) } : c.seen }
    }), TICK_MS)
    return () => window.clearInterval(t)
  }, [live])
  return useMemo(() => ringRecoveries(events ?? [], clock.seen, clock.wall, recoverS, now, speed), [events, clock, recoverS, now, speed])
}
