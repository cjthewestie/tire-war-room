import { useContext, useEffect, useReducer, useState, type ReactNode } from 'react'
import { useHistoryRecorder } from './history'
import { initialState, reduce } from './store'
import { pickTransport, type Transport } from './transport'
import { WarRoomCtx } from './warRoomCtx'

export function WarRoomProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reduce, initialState)
  const [transport, setTransport] = useState<Transport | null>(null)
  const [connected, setConnected] = useState(false)
  useEffect(() => {
    let stop: (() => void) | undefined
    let cancelled = false
    pickTransport().then((t) => {
      if (cancelled) return
      setTransport(t)
      stop = t.start((msg) => dispatch({ kind: 'ws', msg }), setConnected)
    })
    return () => { cancelled = true; stop?.() }
  }, [])
  useHistoryRecorder(state, transport)   // 主控台 今天處理過的事: kept across tab and scenario switches (history.ts)
  return <WarRoomCtx.Provider value={{ state, transport, connected }}>{children}</WarRoomCtx.Provider>
}

export function useWarRoom() { return useContext(WarRoomCtx) }
