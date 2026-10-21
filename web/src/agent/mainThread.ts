// The 主 agent's thread as plain bubbles (issue #54): its turns (audit kind=system actor=main_agent orch=chat, backfilled
// once from GET /api/audit, then live over WS), its execute dispatches, reports and interrupts, and what is being sent now.
// Only what happened since the scenario was last loaded (the runtime's system row {"msg": "scenario loaded"}): the 主控台
// showed earlier rehearsals, and every reload added 「機台處理 agent 已停止：事件處理被取消」.
import { useEffect, useMemo, useState } from 'react'
import type { AuditEntry } from '../contracts'
import { apiGet } from '../transport'
import { useWarRoom } from '../useWarRoom'
import type { LocalItem } from './transcript'

export interface Bubble {
  key: string; ts: string; who: 'me' | 'agent'; text: string; pending?: boolean
  steps?: { label: string; summary: string; ok: boolean }[]; fellBack?: boolean; error?: string | null
  /** the waiting plan (event id) the message was about */
  about?: string | null
}

type D = Record<string, unknown>

const isLoad = (e: AuditEntry) => e.kind === 'system' && (e.payload as D | null)?.msg === 'scenario loaded'
const mine = (e: AuditEntry) => (e.actor === 'main_agent' && e.kind === 'system') || isLoad(e)

/** Audit rows of the 主 agent → bubbles. */
function fromAudit(e: AuditEntry): Bubble[] {
  const p = (e.payload ?? {}) as D
  const o = String(p.orch ?? '')
  if (o === 'chat') {
    const steps = Array.isArray(p.steps) ? (p.steps as D[]).map((s) => ({ label: String(s.label ?? s.tool ?? ''), summary: String(s.summary ?? ''), ok: s.ok !== false })) : []
    const about = (p.selected as string) ?? null
    return [{ key: `a${e.seq}-me`, ts: e.ts, who: 'me', text: String(p.text ?? ''), about },
      { key: `a${e.seq}`, ts: e.ts, who: 'agent', text: String(p.reply ?? ''), steps, fellBack: p.fell_back === true, error: (p.error as string) ?? null, about }]
  }
  // an event the scenario switch (or the day loop) cancelled is not news for the supervisor
  if (o === 'report' && /事件處理被取消/.test(String(p.outcome ?? p.text ?? ''))) return []
  // Agent Nemo 異常報告 (warroom/agent/reporter.py): the whole report, one bubble
  if (o === 'ai_report') return [{ key: `a${e.seq}`, ts: e.ts, who: 'agent', text: String(p.text ?? '') }]
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
    apiGet<AuditEntry[]>('/api/audit?kind=system&limit=5000').then((es) => { if (!off) setBackfill(es.filter(mine)) }).catch(() => {})
    return () => { off = true }
  }, [transport, connected])
  // the store keeps only the last 300 audit rows: the 主 agent's live rows (and the load marker) are kept here, or once
  // the 「scenario loaded」 row scrolled out the cut went back to an older load and the last rehearsal came back mid-demo
  const [kept, setKept] = useState<AuditEntry[]>([])
  const top = kept.length ? kept[kept.length - 1].seq : 0     // kept is sorted by seq
  const add = state.audit.filter((e) => mine(e) && e.seq > top)
  if (add.length) setKept([...kept, ...add].sort((a, b) => a.seq - b.seq))   // adjusted while rendering (no effect)
  return useMemo(() => {
    const bySeq = new Map<number, AuditEntry>()
    let loaded = 0
    for (const e of [...backfill, ...kept, ...state.audit]) {
      if (isLoad(e)) loaded = Math.max(loaded, e.seq)
      else if (e.actor === 'main_agent' && e.kind === 'system') bySeq.set(e.seq, e)
    }
    const all = [...bySeq.values()].sort((a, b) => a.seq - b.seq)
    // every turn the server has, so a message sent before the switch does not come back as a local bubble
    const turns = new Set(all.map((e) => String((e.payload as D)?.turn ?? '')).filter(Boolean))
    const out: Bubble[] = all.filter((e) => e.seq > loaded).flatMap(fromAudit)
    // what is being sent now (and answers whose audit row has not arrived yet)
    for (const it of asks) {
      if (it.k !== 'chat' || (it.turn && turns.has(it.turn))) continue
      out.push({ key: it.key, ts: it.ts, who: it.role === 'me' ? 'me' : 'agent', text: it.text, pending: it.pending, steps: it.steps,
        fellBack: it.fellBack, error: it.error, about: it.about ?? null })
    }
    return out
  }, [backfill, kept, state.audit, asks])
}

