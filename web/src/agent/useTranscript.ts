import { useEffect, useMemo, useState } from 'react'
import type { AuditEntry, Event, ScheduleVersion } from '../contracts'
import { useWarRoom } from '../useWarRoom'
import { fromAudit, fromEvent, type Item } from './transcript'

/** One event's transcript. Live: backfill GET /api/audit?event_id=… once per event (and on reconnect),
 *  then append the WS `audit` entries as they arrive. Mock: rebuilt from the event. */
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
  const live = state.audit
  return useMemo(() => {
    if (!ev) return { items: [], source: 'none' as const }
    const entries = [...(backfill?.id === ev.id ? backfill.entries : []), ...live.filter((e) => e.event_id === ev.id)]
    if (entries.length) return { items: fromAudit(entries, ev), source: 'audit' as const }
    if (transport?.mode === 'mock') return { items: fromEvent(ev), source: 'event' as const }
    return { items: [], source: 'audit' as const }
  }, [ev, backfill, live, transport])
}

/** Schedule versions, refetched whenever the server announces a schedule change. */
export function useVersions(): ScheduleVersion[] {
  const { transport, state } = useWarRoom()
  const [versions, setVersions] = useState<ScheduleVersion[]>([])
  const n = state.pending.length
  useEffect(() => {
    let cancelled = false
    transport?.scheduleVersions().then((v) => { if (!cancelled) setVersions(v) }).catch(() => {})
    return () => { cancelled = true }
  }, [transport, state.scheduleRev, n])
  return versions
}

