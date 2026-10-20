// The 主 agent's thread as plain bubbles (issue #54): its turns (audit kind=system actor=main_agent orch=chat, backfilled
// once from GET /api/audit, then live over WS), its execute dispatches, reports and interrupts, and what is being sent now.
import { useEffect, useMemo, useState } from 'react'
import type { AuditEntry } from '../contracts'
import { apiGet } from '../transport'
import { useWarRoom } from '../useWarRoom'
import type { LocalItem } from './transcript'

export interface Bubble {
  key: string; ts: string; who: 'me' | 'agent'; text: string; pending?: boolean
  steps?: { label: string; summary: string; ok: boolean }[]; fellBack?: boolean; error?: string | null
}

type D = Record<string, unknown>

/** Audit rows of the 主 agent → bubbles. */
function fromAudit(e: AuditEntry): Bubble[] {
  const p = (e.payload ?? {}) as D
  const o = String(p.orch ?? '')
  if (o === 'chat') {
    const steps = Array.isArray(p.steps) ? (p.steps as D[]).map((s) => ({ label: String(s.label ?? s.tool ?? ''), summary: String(s.summary ?? ''), ok: s.ok !== false })) : []
    return [{ key: `a${e.seq}-me`, ts: e.ts, who: 'me', text: String(p.text ?? '') },
      { key: `a${e.seq}`, ts: e.ts, who: 'agent', text: String(p.reply ?? ''), steps, fellBack: p.fell_back === true, error: (p.error as string) ?? null }]
  }
  if (o === 'report' || o === 'interrupt' || (o === 'dispatch' && p.phase === 'execute')) {
    return [{ key: `a${e.seq}`, ts: e.ts, who: 'agent', text: shortText(String(p.text ?? '')) }]
  }
  return []
}

/** Reports are code-built and long; the bubble keeps the first sentence (the Agent page has the rest). */
function shortText(t: string): string {
  const first = t.split('。')[0]
  return first.length > 90 ? `${first.slice(0, 88)}…` : `${first}。`
}

export function useMainThread(asks: LocalItem[]): Bubble[] {
  const { state, transport, connected } = useWarRoom()
  const [backfill, setBackfill] = useState<AuditEntry[]>([])
  useEffect(() => {
    if (transport?.mode !== 'live' || !connected) return
    let off = false
    apiGet<AuditEntry[]>('/api/audit?kind=system&limit=5000').then((es) => { if (!off) setBackfill(es.filter((e) => e.actor === 'main_agent')) }).catch(() => {})
    return () => { off = true }
  }, [transport, connected])
  return useMemo(() => {
    const bySeq = new Map<number, AuditEntry>()
    for (const e of [...backfill, ...state.audit]) if (e.actor === 'main_agent' && e.kind === 'system') bySeq.set(e.seq, e)
    const rows = [...bySeq.values()].sort((a, b) => a.seq - b.seq)
    const turns = new Set(rows.map((e) => String((e.payload as D)?.turn ?? '')).filter(Boolean))
    const out: Bubble[] = rows.flatMap(fromAudit)
    // what is being sent now (and answers whose audit row has not arrived yet)
    for (const it of asks) {
      if (it.k !== 'chat' || (it.turn && turns.has(it.turn))) continue
      out.push({ key: it.key, ts: it.ts, who: it.role === 'me' ? 'me' : 'agent', text: it.text, pending: it.pending, steps: it.steps,
        fellBack: it.fellBack, error: it.error })
    }
    return out
  }, [backfill, state.audit, asks])
}

