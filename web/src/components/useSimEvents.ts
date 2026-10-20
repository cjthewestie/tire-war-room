// 模擬事件 (issue #36): the generator's state (from every WS clock message), its catalog, and the calls that change it.
import { useCallback, useEffect, useState } from 'react'
import type { SimEventDef } from '../contracts'
import { useWarRoom } from '../useWarRoom'

export function useSimEvents() {
  const { state, transport } = useWarRoom()
  const [catalog, setCatalog] = useState<SimEventDef[]>([])
  const [note, setNote] = useState<string | null>(null)
  const live = !!transport?.simEvents
  useEffect(() => {
    if (!transport?.simEvents) return
    transport.simEvents().then((v) => setCatalog(v.catalog), () => setCatalog([]))
  }, [transport])
  const rnd = state.clock?.random_events ?? null
  const toggle = useCallback(async (on?: boolean) => {
    if (!transport?.setSimEvents) return null
    const v = await transport.setSimEvents({ enabled: on ?? !rnd?.enabled })
    setNote(v.enabled ? `隨機事件：開（每 ${v.every_min} 分）` : '隨機事件：關')
    return v
  }, [transport, rnd?.enabled])
  const setEvery = useCallback(async (m: number) => { await transport?.setSimEvents?.({ every_min: m }) }, [transport])
  const fire = useCallback(async (kind: string) => {
    if (!transport?.fireEvent) return null
    const f = await transport.fireEvent(kind)
    setNote(`已觸發：${f.text}`)
    return f
  }, [transport])
  return { live, catalog, rnd, note, toggle, setEvery, fire }
}
