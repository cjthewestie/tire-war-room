// Minimal typed client: REST helpers + one live WebSocket hook. Web builder may extend.
import { useEffect, useRef, useState } from 'react'
import {
  API, apiPath,
  type ApprovalDecision, type ApprovalRequest, type DashboardSnapshot, type Event, type MachineTile,
  type WsMessage,
} from './contracts'

export async function getJSON<T>(path: string): Promise<T> {
  const r = await fetch(path)
  if (!r.ok) throw new Error(`${r.status} ${path}`)
  return (await r.json()) as T
}

export async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!r.ok) throw new Error(`${r.status} ${path}`)
  return (await r.json()) as T
}

export function decide(d: ApprovalDecision): Promise<ApprovalRequest> {
  return postJSON(apiPath(API.approvalDecision, { request_id: d.request_id }), d)
}

function wsUrl(): string {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.host}${API.ws}`
}

/** Keeps the latest snapshot and patches it with incremental WS messages. Auto-reconnects. */
export function useLiveStream() {
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null)
  const [connected, setConnected] = useState(false)
  const [pending, setPending] = useState<ApprovalRequest[]>([])
  const [lastMessage, setLastMessage] = useState<WsMessage | null>(null)
  const retry = useRef(0)

  useEffect(() => {
    let ws: WebSocket | null = null
    let closed = false
    let timer: ReturnType<typeof setTimeout> | undefined

    const connect = () => {
      ws = new WebSocket(wsUrl())
      ws.onopen = () => { setConnected(true); retry.current = 0 }
      ws.onclose = () => {
        setConnected(false)
        if (!closed) timer = setTimeout(connect, Math.min(5000, 500 * 2 ** retry.current++))
      }
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data) as WsMessage
        setLastMessage(msg)
        switch (msg.type) {
          case 'snapshot':
            setSnapshot(msg.data)
            setPending(msg.data.pending_approvals)
            break
          case 'machines':
            setSnapshot((s) => (s ? { ...s, machines: msg.data.machines as MachineTile[] } : s))
            break
          case 'event':
            setSnapshot((s) => (s ? { ...s, events: upsert(s.events, msg.data) } : s))
            break
          case 'approval_request':
            setPending((p) => [...p.filter((a) => a.id !== msg.data.id), msg.data])
            break
          case 'approval_resolved':
            setPending((p) => p.filter((a) => a.id !== msg.data.id))
            break
        }
      }
    }
    getJSON<DashboardSnapshot>(API.snapshot).then((s) => { setSnapshot(s); setPending(s.pending_approvals) }).catch(() => {})
    connect()
    return () => { closed = true; clearTimeout(timer); ws?.close() }
  }, [])

  return { snapshot, connected, pending, lastMessage }
}

function upsert(list: Event[], e: Event): Event[] {
  const i = list.findIndex((x) => x.id === e.id)
  if (i < 0) return [e, ...list]
  const copy = list.slice()
  copy[i] = e
  return copy
}
