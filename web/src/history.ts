// 今天處理過的事 (主控台 home overview; IA prototype v12: 「沒事時不是空白：告訴你下一件會發生的事」): one row per event the
// plant closed and per approval decision, kept on the client for the page session. A scenario switch rebuilds the whole
// simulation (live runtime.py, mock engine.ts) and its events vanish, so the rows already seen stay here, marked 上一輪.
// Built only from what the state already carries: snapshot.events, resolved approvals and (live) the audit stream.
// Kept outside React (like replanStore.ts) and fed by WarRoomProvider, so it records on every tab, not only 主控台.
import { useEffect, useSyncExternalStore } from 'react'
import type { ApprovalRequest, AuditEntry, Event } from './contracts'
import { machineLabel } from './factory'
import { plain } from './plain'
import type { WarRoomState } from './store'
import type { Transport } from './transport'

export type HistTone = 'ok' | 'stop' | 'back' | 'decide' | 'no'
export interface HistRow {
  key: string                 // `${run}:e:<event id>` or `${run}:d:<approval id>`
  run: number                 // which simulation run (bumps on a scenario switch)
  kind: 'event' | 'decision'
  at: string                  // factory time the event closed / the decision was made
  eventId: string | null
  subject: string             // 「7 號硫化機」「主管指令」
  text: string                // event title, or 「主管核准 B 從 2 線調 1 人」
  tag: string                 // 結案／安全停止／已回復, or 核准／修改後核准／駁回／逾時
  tone: HistTone
  detail: string | null       // the event's outcome
}
export interface History { run: number; rows: HistRow[] }

const MAX = 30
const DECISION: Record<string, string> = { approve: '核准', modify: '修改後核准', reject: '駁回', expired: '逾時未決定' }
const STATUS_DECISION: Partial<Record<ApprovalRequest['status'], string>> = { approved: 'approve', modified: 'modify', rejected: 'reject', expired: 'expired' }
const VIA: Record<string, string> = { phone: '手機', touchscreen: '現場螢幕' }

export const subjectOf = (e: Event) => (e.source === 'supervisor' ? '主管指令' : e.source === 'plant' ? '工廠事件' : machineLabel(e.machine_id))
export const titleOf = (e: Event) => plain(e.alert_card?.title ?? e.command ?? e.anomaly.message)
const who = (by: string | null | undefined) => (!by || by === 'supervisor' || by === 'dashboard' ? '主管' : by === 'system' ? '系統' : by)

/** 結案 / 安全停止 / 已回復 (an event that settled by itself, e.g. 「自行回穩」, needed nothing done). */
function eventRow(run: number, e: Event): HistRow {
  const stop = e.state === 'safe_stop'
  const back = !stop && /自行回穩|已回復|恢復正常/.test(e.outcome ?? '')
  return {
    key: `${run}:e:${e.id}`, run, kind: 'event', at: e.closed_at ?? e.timeline[e.timeline.length - 1]?.ts ?? e.opened_at, eventId: e.id,
    subject: subjectOf(e), text: titleOf(e), tag: stop ? '安全停止' : back ? '已回復' : '結案', tone: stop ? 'stop' : back ? 'back' : 'ok',
    detail: e.outcome ? plain(e.outcome) : null,
  }
}

/** 「核准 B 從 2 線調 1 人」: the option the supervisor picked (its letter when the card offered more than one). */
export function decisionText(r: ApprovalRequest | undefined, decision: string, optionId: string | null | undefined): string {
  const verb = DECISION[decision] ?? decision
  const opt = r?.options.find((o) => o.id === optionId)
  if (decision === 'reject' || decision === 'expired' || !opt) return `${verb}${r ? `「${plain(r.title)}」` : optionId ? ` ${optionId} 案` : ''}`
  return `${verb} ${r && r.options.length > 1 ? `${opt.id} ` : ''}${plain(opt.label.replace(/^核准/, ''))}`
}

function decisionRow(run: number, r: ApprovalRequest | undefined, a: { id: string; event_id: string; decision: string; option_id?: string | null; by?: string | null; via?: string | null; at: string }, events: Event[]): HistRow {
  const ev = events.find((e) => e.id === a.event_id)
  return {
    key: `${run}:d:${a.id}`, run, kind: 'decision', at: a.at, eventId: a.event_id,
    subject: ev ? subjectOf(ev) : '', text: `${who(a.by)}${decisionText(r, a.decision, a.option_id)}${a.via && VIA[a.via] ? `（${VIA[a.via]}）` : ''}`,
    tag: DECISION[a.decision] ?? a.decision, tone: a.decision === 'reject' || a.decision === 'expired' ? 'no' : 'decide', detail: null,
  }
}

// ---------------- the store ----------------
let hist: History = { run: 1, rows: [] }
const subs = new Set<() => void>()
let lastScenario: string | undefined
let lastEvents: Event[] | null = null
let seenIds = new Set<string>()
let waitNewList = false                       // a scenario switch started the run; the old list is still on screen
let staleEvents = new WeakSet<Event>()        // the previous run's event objects (mock reuses event ids across runs)
let staleResolved = new WeakSet<ApprovalRequest>()   // the store keeps resolved approvals across runs
let auditFrom = -1                            // live keeps the audit across scenarios: only entries after the switch
const backfilled = new Set<Transport>()

function newRun(s: WarRoomState, switched: boolean) {
  const evs = s.snapshot?.events ?? []
  hist = { ...hist, run: hist.run + 1 }
  staleResolved = new WeakSet(s.resolved)
  staleEvents = switched ? new WeakSet(evs) : new WeakSet()
  waitNewList = switched
  auditFrom = s.audit.reduce((m, a) => Math.max(m, a.seq), auditFrom)
}

/** Fold the current state into the history; detects a new run (live: clock.scenario changed; both: every event of the
 *  previous list vanished at once, as a scenario load or ↺ 重來 does). */
export function recordHistory(s: WarRoomState) {
  const evs = s.snapshot?.events
  if (!evs) return
  const before = hist
  const sc = s.clock?.scenario
  if (sc && lastScenario && sc !== lastScenario) newRun(s, true)
  if (sc) lastScenario = sc
  if (evs !== lastEvents) {
    const ids = new Set(evs.map((e) => e.id))
    const vanished = seenIds.size > 0 && ![...seenIds].some((id) => ids.has(id))
    if (vanished) { if (waitNewList) waitNewList = false; else newRun(s, false) }
    else if (waitNewList && !evs.some((e) => staleEvents.has(e))) waitNewList = false
    seenIds = ids; lastEvents = evs
  }
  const run = hist.run
  const fresh: HistRow[] = []
  const live = evs.filter((e) => !staleEvents.has(e))
  for (const e of live) if (e.state === 'closed' || e.state === 'safe_stop') fresh.push(eventRow(run, e))
  const byReq = new Map<string, ApprovalRequest>()
  for (const r of [...s.pending, ...s.resolved]) if (!staleResolved.has(r)) byReq.set(r.id, r)
  // live: the audit entry is the record of who decided and when; the approval card carries the option labels
  const audited = new Set<string>()
  for (const a of s.audit) {
    if (a.seq <= auditFrom || a.kind !== 'approval_decision' || !a.event_id) continue
    const p = a.payload as { request_id?: string; decision?: string; option_id?: string | null; via?: string | null }
    if (!p.request_id || !p.decision || !DECISION[p.decision]) continue
    audited.add(p.request_id)
    fresh.push(decisionRow(run, byReq.get(p.request_id), { id: p.request_id, event_id: a.event_id, decision: p.decision, option_id: p.option_id, by: a.actor, via: p.via, at: a.ts }, live))
  }
  for (const r of s.resolved) {
    const d = STATUS_DECISION[r.status]
    if (staleResolved.has(r) || !d || audited.has(r.id)) continue
    fresh.push(decisionRow(run, r, { id: r.id, event_id: r.event_id, decision: d, option_id: r.decision?.option_id, by: r.decision?.decided_by, via: r.decision?.via, at: r.decision?.decided_at ?? r.created_at }, live))
  }
  upsert(fresh)
  if (hist !== before) for (const f of subs) f()
}

/** Live, on page load: decisions made before this page opened are in GET /api/audit, not in the WS stream. */
async function backfill(t: Transport, s: WarRoomState) {
  const entries: AuditEntry[] = await t.audit().catch(() => [])
  const ids = new Set((s.snapshot?.events ?? []).map((e) => e.id))
  const known = new Set(s.audit.map((a) => a.seq))
  const mine = entries.filter((a) => a.kind === 'approval_decision' && a.event_id && ids.has(a.event_id) && !known.has(a.seq))
  if (mine.length) recordHistory({ ...s, audit: [...mine, ...s.audit] })
}

function upsert(fresh: HistRow[]) {
  let rows = hist.rows
  for (const r of fresh) {
    const i = rows.findIndex((x) => x.key === r.key)
    if (i >= 0 && same(rows[i], r)) continue
    rows = i >= 0 ? rows.map((x, j) => (j === i ? r : x)) : [...rows, r]
  }
  if (rows === hist.rows) return
  rows = rows.slice().sort((a, b) => b.run - a.run || (a.at < b.at ? 1 : a.at > b.at ? -1 : 0)).slice(0, MAX)
  hist = { ...hist, rows }
}
const same = (a: HistRow, b: HistRow) => a.at === b.at && a.text === b.text && a.tag === b.tag && a.subject === b.subject && a.detail === b.detail

export function useHistory(): History {
  return useSyncExternalStore((f) => { subs.add(f); return () => { subs.delete(f) } }, () => hist)
}

/** WarRoomProvider: record every state change (cheap: a few events and approvals), and backfill once in live. */
export function useHistoryRecorder(state: WarRoomState, transport: Transport | null) {
  useEffect(() => { recordHistory(state) }, [state])
  useEffect(() => {   // once per transport, with the first snapshot
    if (!state.snapshot || !transport || transport.mode !== 'live' || backfilled.has(transport)) return
    backfilled.add(transport)
    void backfill(transport, state)
  }, [state, transport])
}

/** The latest human decision of this run on an event (the result strip follows it); rows are newest first. */
export function latestDecision(h: History, eventId: string): HistRow | null {
  return h.rows.find((r) => r.run === h.run && r.kind === 'decision' && r.eventId === eventId && r.tag !== DECISION.expired) ?? null
}
