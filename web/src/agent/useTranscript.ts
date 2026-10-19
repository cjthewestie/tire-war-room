import { useEffect, useMemo, useState } from 'react'
import type { AuditEntry, Event, ScheduleVersion } from '../contracts'
import type { Transport } from '../transport'
import { useWarRoom } from '../useWarRoom'
import { fromAudit, fromEvent, type Item } from './transcript'

const NONE: AuditEntry[] = []

/** One event's transcript. Live: backfill GET /api/audit?event_id=… once per event (and on reconnect),
 *  then keep every WS `audit` entry of that event as it arrives — kept here by seq, so the store's
 *  short global window (last 300 entries, all events) never drops rows from a long transcript.
 *  Mock: rebuilt from the event. */
export function useTranscript(ev: Event | null): { items: Item[]; source: 'audit' | 'event' | 'none' } {
  const { state, transport, connected } = useWarRoom()
  const [backfill, setBackfill] = useState<{ id: string; entries: AuditEntry[] } | null>(null)
  const id = ev?.id ?? null
  useEffect(() => {
    if (!id || !transport?.eventAudit || !connected) return
    let cancelled = false
    transport.eventAudit(id).then((entries) => { if (!cancelled) setBackfill({ id, entries }) }).catch(() => {})
    return () => { cancelled = true }
  }, [id, transport, connected])
  // live entries of this event, accumulated (the store slices its window; we don't). Updated while
  // rendering when the store's window changes — React's "adjust state on prop change" pattern.
  const live = state.audit
  const [acc, setAcc] = useState<{ id: string | null; seen: AuditEntry[] | null; kept: AuditEntry[] }>({ id: null, seen: null, kept: [] })
  if (acc.id !== id || acc.seen !== live) {
    const prev = acc.id === id ? acc.kept : NONE
    const bySeq = new Map(prev.map((e) => [e.seq, e]))
    for (const e of live) if (e.event_id === id) bySeq.set(e.seq, e)
    setAcc({ id, seen: live, kept: bySeq.size === prev.length ? prev : [...bySeq.values()] })
  }
  const kept = acc.id === id ? acc.kept : NONE
  return useMemo(() => {
    if (!ev) return { items: [], source: 'none' as const }
    const entries = [...(backfill?.id === ev.id ? backfill.entries : []), ...kept]
    if (entries.length) return { items: fromAudit(entries, ev), source: 'audit' as const }
    if (transport?.mode === 'mock') return { items: fromEvent(ev), source: 'event' as const }
    return { items: [], source: 'audit' as const }
  }, [ev, backfill, kept, transport])
}

// one GET /api/schedule/versions per schedule change, shared by every card on the page
const versionCache = new WeakMap<Transport, { key: string; p: Promise<ScheduleVersion[]> }>()
function fetchVersions(t: Transport, key: string): Promise<ScheduleVersion[]> {
  const hit = versionCache.get(t)
  if (hit && hit.key === key) return hit.p
  const p = t.scheduleVersions()
  versionCache.set(t, { key, p })
  p.catch(() => { if (versionCache.get(t)?.p === p) versionCache.delete(t) })
  return p
}

/** Schedule versions, refetched whenever the server announces a schedule change. */
export function useVersions(): ScheduleVersion[] {
  const { transport, state } = useWarRoom()
  const [versions, setVersions] = useState<ScheduleVersion[]>([])
  const key = `${state.scheduleRev}:${state.pending.map((p) => p.id).join(',')}`
  useEffect(() => {
    if (!transport) return
    let cancelled = false
    fetchVersions(transport, key).then((v) => { if (!cancelled) setVersions(v) }).catch(() => {})
    return () => { cancelled = true }
  }, [transport, key])
  return versions
}
