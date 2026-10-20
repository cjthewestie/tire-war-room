// The header chip (CJ, 2026-10-20: 「那個框框要顯示連線中」): one word for the link, details in 後台 → 狀態.
//   連線中         green  live WebSocket, LLM mode, the model answers and the commander's last plan was its own
//   模型未連線     amber  rule mode, the model is unreachable (GET /api/health), or the last plan fell back to rules
//   離線・示範資料 amber  the in-browser mock (no backend)
//   重新連線…     red    the WebSocket is reconnecting
import { useEffect, useState } from 'react'
import type { HealthStatus } from './contracts'
import { apiGet } from './transport'
import { useWarRoom } from './useWarRoom'

export type LinkTone = 'ok' | 'warn' | 'mock' | 'down'
export interface LinkStatus { text: string; tone: LinkTone; title: string }

export function useLinkStatus(): LinkStatus {
  const { state, transport, connected } = useWarRoom()
  const live = transport?.mode === 'live'
  const [health, setHealth] = useState<HealthStatus | null>(null)
  useEffect(() => {
    if (!live || !connected) return
    let off = false
    const load = () => apiGet<HealthStatus>('/api/health').then((h) => { if (!off) setHealth(h) }).catch(() => {})
    load()
    const t = setInterval(load, 20_000)
    return () => { off = true; clearInterval(t) }
  }, [live, connected])
  if (!transport) return { text: '重新連線…', tone: 'down', title: '正在連線戰情室後端' }
  if (!live) return { text: '離線・示範資料', tone: 'mock', title: '瀏覽器內模擬資料（沒有連線後端）' }
  if (!connected) return { text: '重新連線…', tone: 'down', title: 'WebSocket 中斷，自動重新連線中' }
  const lastPlan = [...state.audit].reverse().find((e) => e.kind === 'llm_call' && e.actor === 'commander')
  const fellBack = lastPlan?.payload?.fell_back_to_mock === true
  const mode = health?.llm_mode ?? state.snapshot?.header.llm_mode
  if (mode !== 'llm') return { text: '模型未連線', tone: 'warn', title: '規則模式：指揮官不呼叫模型（詳細見後台 → 狀態）' }
  if (health && !health.llm_reachable) return { text: '模型未連線', tone: 'warn', title: `模型 ${health.model} 連不上，指揮官改用規則（詳細見後台 → 狀態）` }
  if (fellBack) return { text: '模型未連線', tone: 'warn', title: '指揮官上一次的計劃改用規則備援（模型輸出沒通過檢查；詳細見後台 → 狀態）' }
  return { text: '連線中', tone: 'ok', title: `已連線・${health?.model ?? 'Nemotron'}（詳細見後台 → 狀態）` }
}
