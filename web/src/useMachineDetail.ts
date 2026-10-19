// Live MachineDetail (history, tiles, prediction) for one machine, refetched on each factory tick.
import { useEffect, useState } from 'react'
import type { MachineDetail } from './contracts'
import { useWarRoom } from './useWarRoom'

export function useMachineDetail(id: string | null | undefined): MachineDetail | null {
  const { transport, state } = useWarRoom()
  const [d, setD] = useState<MachineDetail | null>(null)
  const tick = state.clock?.factory_time
  useEffect(() => {
    if (!id || !transport) return
    let alive = true
    transport.machineDetail(id).then((x) => { if (alive) setD(x) }).catch(() => {})
    return () => { alive = false }
    // refetch on every factory tick: mock ticks per second, live per sim step
  }, [id, transport, tick])
  return d
}
