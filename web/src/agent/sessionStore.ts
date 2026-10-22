// One Agent Nemo session per task (issue #189; CJ, 2026-10-22: 「一個任務一個 agent nemo 對話，類似一個 session 的概念」).
// A session id is the task's event id; null is the 「新對話」 session (a message with no task). The task list behind the
// switcher comes from GET /api/tasks (進行中, 今日已結案, 歷史任務 by factory day; warroom/api/task_sessions.py), searched
// on the server; in the browser-only mock it is built from the snapshot's events. A past task's turns come from the
// chat log on disk (GET /api/tasks/{id}/session), read-only.
import { useEffect, useState } from 'react'
import type { Event, TaskListing, TaskRow, TaskSessionTurn } from '../contracts'
import { machineLabel } from '../factory'
import { apiGet } from '../transport'
import { useWarRoom } from '../useWarRoom'
import { STATE_LABEL } from '../components/common'
import type { Bubble } from './mainThread'

const ended = (e: Event) => e.state === 'closed' || e.state === 'safe_stop'
const KIND: Record<string, string> = { detector: '機台異常', supervisor: '主管指令', plant: '工廠事件' }

function rowOf(e: Event): TaskRow {
  const who = e.source === 'supervisor' ? '主管指令' : e.source === 'plant' ? '工廠事件' : machineLabel(e.machine_id)
  const what = (e.command ?? e.alert_card?.title ?? e.anomaly.message ?? '').slice(0, 40)
  return {
    id: e.id, title: what.startsWith(who) ? what.slice(0, 24) : `${who}・${what.slice(0, 24)}`, what, kind: e.source ?? 'detector',
    kind_label: KIND[e.source ?? 'detector'] ?? '任務', machine_id: e.machine_id === 'PLANT' ? null : e.machine_id,
    machine_label: e.machine_id === 'PLANT' ? null : machineLabel(e.machine_id), orders: [...what.matchAll(/#\s?[a-z]-?\d{2,4}|SO-\d{2,4}/gi)].map((m) => m[0].toUpperCase()),
    state: e.state, state_label: STATE_LABEL[e.state], opened_at: e.opened_at, closed_at: e.closed_at ?? null, outcome: e.outcome ?? null,
    live: true, running: !ended(e),
  }
}

/** Search: every word must appear in the title, id, machine, orders, type, state or outcome (case, '#', spaces ignored). */
export function matches(r: TaskRow, q: string): boolean {
  const norm = (s: string) => s.replace(/[\s#]/g, '').toUpperCase()
  const hay = norm([r.id, r.title, r.what, r.machine_id, r.machine_label, r.orders.join(' '), r.kind_label, r.state_label, r.outcome].filter(Boolean).join(' '))
  return q.split(/\s+/).filter(Boolean).every((w) => hay.includes(norm(w)))
}

/** The mock (no backend): this scenario's events only; nothing is kept across a reload, so there is no history. */
export function localListing(events: Event[], q: string, today: string): TaskListing {
  const rows = [...events].sort((a, b) => (a.opened_at < b.opened_at ? 1 : -1)).map(rowOf).filter((r) => matches(r, q))
  return { today: { date: today, label: today.slice(5).replace('-', '/') }, q, running: rows.filter((r) => r.running), closed_today: rows.filter((r) => !r.running), history: [] }
}

/** The task list for the switcher / the Agent page, refreshed every 6 s and on a new search (debounced). */
export function useTaskListing(q: string): TaskListing | null {
  const { state, transport, connected } = useWarRoom()
  const live = transport?.mode === 'live'
  const [data, setData] = useState<TaskListing | null>(null)
  const [query, setQuery] = useState(q)
  useEffect(() => { const t = setTimeout(() => setQuery(q.trim()), 250); return () => clearTimeout(t) }, [q])
  useEffect(() => {
    if (!live || !connected) return
    let off = false
    const pull = () => apiGet<TaskListing>(`/api/tasks${query ? `?q=${encodeURIComponent(query)}` : ''}`).then((d) => { if (!off) setData(d) }).catch(() => {})
    void pull()
    const t = setInterval(pull, 6000)
    return () => { off = true; clearInterval(t) }
  }, [live, connected, query])
  if (!live) return localListing(state.snapshot?.events ?? [], q.trim(), (state.clock?.factory_time ?? state.snapshot?.header.factory_time ?? '').slice(0, 10))
  return data
}

/** Every row of a listing, for a lookup by id. */
export const allRows = (l: TaskListing | null): TaskRow[] => (l ? [...l.running, ...l.closed_today, ...l.history.flatMap((g) => g.tasks)] : [])

/** A past task's turns from the chat log on disk, as bubbles (read-only). */
export function useStoredSession(eventId: string | null): { bubbles: Bubble[]; loaded: boolean } {
  const { transport, connected } = useWarRoom()
  const [out, setOut] = useState<{ id: string | null; bubbles: Bubble[] }>({ id: null, bubbles: [] })
  useEffect(() => {
    if (!eventId || transport?.mode !== 'live' || !connected) return
    let off = false
    apiGet<{ turns: TaskSessionTurn[] }>(`/api/tasks/${encodeURIComponent(eventId)}/session`).then((d) => {
      if (off) return
      setOut({ id: eventId, bubbles: d.turns.map((t) => ({ key: `h${t.id}`, ts: t.ts, who: t.role === 'user' ? 'me' : 'agent', text: t.text, about: eventId })) })
    }).catch(() => { if (!off) setOut({ id: eventId, bubbles: [] }) })
    return () => { off = true }
  }, [eventId, transport, connected])
  return { bubbles: out.id === eventId ? out.bubbles : [], loaded: out.id === eventId }
}
