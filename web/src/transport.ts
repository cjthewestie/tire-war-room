// One interface, two backends: the real FastAPI (REST + /ws) or the in-browser MockEngine.
// Mock mode syncs tabs over BroadcastChannel, so approving on /phone updates the dashboard.
import type { OrdersToday } from './components/ordersToday'
import {
  API, apiPath,
  type ApprovalDecision, type ApprovalRequest, type AskAnswer, type AuditEntry, type CapacityReport, type CommandReply, type DashboardSnapshot, type DiscussReply, type Event,
  type MachineDetail, type OverlayResult, type SafetyCase, type SafetyRelease, type ScenarioName, type ScheduleVersion, type Shift, type SimControl, type SimEventFired, type SimEventsBody,
  type SimEventsView, type SubagentsView, type MainReply, type WorldSnapshot, type WsMessage,
} from './contracts'
import { MockEngine, type Network } from './mock/engine'
import { CURRENT_SHIFTS } from './factory'
import { shiftInstances } from './mock/schedule'

/** Agent Nemo's conversation for this page load (POST /api/main/message conversation_id). */
const CONVERSATION_ID = `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`

export type Mode = 'live' | 'mock'

/** GET /api/schedule/rollback/preview: what a rollback would do and whether the gate allows it. */
export interface RollbackPreview {
  from_version: number
  to_version: number
  target_status: string
  target_reason: string
  feasible: boolean
  reasons: string[]
  risk: 'R2' | 'R3'
  moved: { job_id: string; order_id: string; from_press: string; to_press: string; to_mold_in: string }[]
  moved_count: number
  added: number
  removed: number
  staffing_changes: string[]
}

export interface Transport {
  mode: Mode
  start(onMsg: (m: WsMessage) => void, onConn: (c: boolean) => void): () => void
  decide(d: ApprovalDecision): Promise<ApprovalRequest | null>
  control(c: SimControl): Promise<void>
  /** 情境疊加: apply an overlayable scenario on top of the running one from now ({action:'overlay'}); what it added. */
  overlay(scenario: ScenarioName): Promise<OverlayResult>
  /** confirm=true is the supervisor's explicit confirmation (the R2/R3 approval); live only checks it. */
  rollback(version?: number, confirm?: boolean): Promise<ScheduleVersion | null>
  /** Live only: the gate's preview of a rollback (diff + block reasons). */
  rollbackPreview?(version?: number): Promise<RollbackPreview>
  machineDetail(id: string): Promise<MachineDetail>
  scheduleVersions(): Promise<ScheduleVersion[]>
  shifts(): Promise<Shift[]>
  currentShifts(): Promise<Shift[]>
  capacity(): Promise<CapacityReport>
  audit(): Promise<AuditEntry[]>
  /** Live only: every audit entry of one event (GET /api/audit?event_id=…), for the agent console backfill. */
  eventAudit?(eventId: string): Promise<AuditEntry[]>
  /** Simulate the offline tiers (page 9). Live: POST /api/sim/network; the backend then reports `header.network`. */
  setNetwork(n: Network): Promise<void>
  /** Mock only: jump factory time. */
  jump?(t: number): Promise<void>
  /** Live only: 問戰情室, a read-only question answered from the read tools (POST /api/ask). */
  ask?(question: string, conversationId?: string): Promise<AskAnswer>
  /** Live only (issue #95): today's orders from the same rules 問戰情室 answers with (GET /api/orders/today). */
  ordersToday?(): Promise<OrdersToday>
  /** Live only (issue #10): 下新任務, a task in plain Chinese that opens an event (POST /api/command). */
  command?(text: string): Promise<CommandReply>
  /** Live only (issue #10): 討論這個計劃, talk about an event's waiting plan (POST /api/events/{id}/discuss). */
  discuss?(eventId: string, text: string): Promise<DiscussReply>
  /** Live only (fast repair): 知道了 on the 異常報告已產生 notice = 主管已讀報告 (POST /api/events/{id}/review), PIN like
   *  approvals. 409 when the event has no unread report. */
  review?(eventId: string, note?: string): Promise<Event>
  /** Live only: resolution paths (warroom/api/resolve.py), supervisor session like approvals. */
  safetyDispatch?(caseId: string): Promise<SafetyCase>
  safetyRelease?(caseId: string, body: SafetyRelease): Promise<SafetyCase>
  stopReplan?(eventId: string): Promise<Event>
  stopHandoff?(eventId: string): Promise<Event>
  stopClose?(eventId: string, note?: string): Promise<Event>
  /** Live only (issue #54): stop a subagent safely (POST /api/subagents/{id}/interrupt). */
  interrupt?(eventId: string): Promise<Record<string, unknown>>
  /** Live only (issue #54): one input to the 主 agent, which classifies the message (POST /api/main/message). */
  message?(text: string, eventId: string | null): Promise<MainReply>
  /** Live only (issue #36 模擬事件): the generator's settings and log, change them, fire one event now. */
  simEvents?(): Promise<SimEventsView>
  setSimEvents?(b: SimEventsBody): Promise<SimEventsView>
  fireEvent?(kind: string): Promise<SimEventFired>
  /** Live only (#72): 現場處置完成 in the 3D factory, remove the injected faults on one machine (POST /api/sim/clear). */
  clearFault?(machineId: string): Promise<void>
}

// ---------------- live ----------------
async function getJSON<T>(path: string): Promise<T> {
  const r = await fetch(path)
  if (!r.ok) throw new Error(`${r.status} ${path}${await errDetail(r)}`)
  return (await r.json()) as T
}
/** Live read-only GET for pages outside the Transport interface (狀態機, 每日事件紀錄). */
export const apiGet = getJSON

// Writes from a LAN client (the phone) need the supervisor PIN (server: WARROOM_SUPERVISOR_PIN).
// It is entered when asked and sent only in the X-Supervisor-Pin header: never read from or put in a
// URL (history, logs), and kept for this tab only (sessionStorage), not persisted in the browser.
const PIN_KEY = 'warroom.pin'
function supervisorPin(): string | null {
  try {
    try { localStorage.removeItem(PIN_KEY) } catch { /* older builds kept it here */ }
    return sessionStorage.getItem(PIN_KEY)
  } catch { return null }
}
async function errDetail(r: Response): Promise<string> {
  try {
    const j = await r.json()
    const d = j?.detail
    if (typeof d === 'string') return `：${d}`
    if (d?.message) return `：${d.message}`
  } catch { /* not JSON */ }
  return ''
}
async function postJSON<T>(path: string, body: unknown, retried = false): Promise<T> {
  const pin = supervisorPin()
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (pin) headers['X-Supervisor-Pin'] = pin
  const r = await fetch(path, { method: 'POST', headers, body: JSON.stringify(body) })
  if (r.status === 401) { try { sessionStorage.removeItem(PIN_KEY) } catch { /* private mode */ } }
  if (r.status === 401 && !retried && typeof window !== 'undefined' && window.prompt) {
    const p = window.prompt('請輸入主管 PIN（廠內 LAN 核准）')
    if (p) {
      try { sessionStorage.setItem(PIN_KEY, p) } catch { /* private mode */ }
      return postJSON<T>(path, body, true)
    }
  }
  if (!r.ok) throw new Error(`${r.status}${await errDetail(r)}`)
  return (await r.json()) as T
}

/** 後台 (issue #37): every admin call, reads included, carries the supervisor PIN when this tab has one and
 *  asks for it on a 401 — same PIN rule as approvals, plus the server's factory-network check. */
export async function adminFetch<T>(path: string, init: { method?: string; body?: unknown } = {}, retried = false): Promise<T> {
  const pin = supervisorPin()
  const headers: Record<string, string> = {}
  if (init.body !== undefined) headers['Content-Type'] = 'application/json'
  if (pin) headers['X-Supervisor-Pin'] = pin
  const r = await fetch(path, { method: init.method ?? 'GET', headers, body: init.body === undefined ? undefined : JSON.stringify(init.body) })
  if (r.status === 401) { try { sessionStorage.removeItem(PIN_KEY) } catch { /* private mode */ } }
  if (r.status === 401 && !retried && typeof window !== 'undefined' && window.prompt) {
    const p = window.prompt('後台需要主管 PIN')
    if (p) {
      try { sessionStorage.setItem(PIN_KEY, p) } catch { /* private mode */ }
      return adminFetch<T>(path, init, true)
    }
  }
  if (!r.ok) throw new Error(`${r.status}${await errDetail(r)}`)
  return (await r.json()) as T
}

export class LiveTransport implements Transport {
  mode: Mode = 'live'
  start(onMsg: (m: WsMessage) => void, onConn: (c: boolean) => void) {
    let ws: WebSocket | null = null
    let closed = false
    let retry = 0
    let timer: ReturnType<typeof setTimeout> | undefined
    const pullSnapshot = () => getJSON<DashboardSnapshot>(API.snapshot)
      .then((s) => onMsg({ type: 'snapshot', ts: s.header.factory_time, data: s })).catch(() => {})
    // the whole plant: full state from GET /api/world (start and every reconnect), then WS "world" deltas
    const pullWorld = () => getJSON<WorldSnapshot>(API.world)
      .then((w) => onMsg({ type: 'world', ts: w.factory_time, data: { ...w, full: true } })).catch(() => {})
    // the active schedule: at start and on every reconnect (a scenario switch while disconnected sent its WS "schedule"
    // to nobody, and the previous scenario's version stayed on screen), then WS "schedule" messages
    const pullSchedule = () => getJSON<ScheduleVersion>(API.schedule)
      .then((v) => onMsg({ type: 'schedule', ts: v.created_at, data: v })).catch(() => {})
    let soon: ReturnType<typeof setTimeout> | undefined
    const soonSnapshot = () => { if (!soon) soon = setTimeout(() => { soon = undefined; pullSnapshot() }, 250) }
    // issue #54: the main agent's subagents (status follows every state change; the server also pushes WS "subagents")
    const pullSubagents = () => getJSON<SubagentsView>('/api/subagents')
      .then((d) => onMsg({ type: 'subagents', ts: new Date().toISOString(), data: d })).catch(() => {})
    let soonS: ReturnType<typeof setTimeout> | undefined
    const soonSubagents = () => { if (!soonS) soonS = setTimeout(() => { soonS = undefined; pullSubagents() }, 300) }
    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      ws = new WebSocket(`${proto}://${location.host}${API.ws}`)
      ws.onopen = () => { onConn(true); retry = 0; pullSnapshot(); pullWorld(); pullSchedule(); pullSubagents() }
      ws.onclose = () => {
        onConn(false)
        if (!closed) timer = setTimeout(connect, Math.min(5000, 500 * 2 ** retry++))
      }
      ws.onmessage = (e) => {
        let m: WsMessage
        try { m = JSON.parse(e.data) as WsMessage } catch { return /* ignore malformed */ }
        onMsg(m)
        // the focus panel (③) is server-built: refresh it as soon as an event or approval changes
        if (m.type === 'event' || m.type === 'approval_request' || m.type === 'approval_resolved' ||
            m.type === 'anomaly' || m.type === 'safety_action') soonSnapshot()
        if (m.type === 'event' || m.type === 'approval_request' || m.type === 'approval_resolved') soonSubagents()
      }
    }
    pullSnapshot()
    pullSchedule()
    connect()
    // safety net: focus panel and counts come from snapshots; refresh in case the server only sends deltas
    const poll = setInterval(() => { pullSnapshot(); pullSubagents() }, 4000)   // subagent progress follows worker tasks too
    return () => { closed = true; clearTimeout(timer); clearInterval(poll); ws?.close() }
  }
  decide(d: ApprovalDecision) { return postJSON<ApprovalRequest>(apiPath(API.approvalDecision, { request_id: d.request_id }), d) }
  ask(question: string, conversationId?: string) { return postJSON<AskAnswer>(API.ask, { question, conversation_id: conversationId ?? null }) }
  ordersToday() { return getJSON<OrdersToday>(API.ordersToday) }
  command(text: string) { return postJSON<CommandReply>(API.command, { text }) }
  discuss(eventId: string, text: string) { return postJSON<DiscussReply>(apiPath(API.discuss, { event_id: eventId }), { text }) }
  review(eventId: string, note?: string) { return postJSON<Event>(apiPath(API.review, { event_id: eventId }), note ? { note } : {}) }
  // one conversation per page load (like #99's ask): Agent Nemo's memory is kept per conversation, never shared
  message(text: string, eventId: string | null) { return postJSON<MainReply>('/api/main/message', { text, event_id: eventId, conversation_id: CONVERSATION_ID }) }
  safetyDispatch(caseId: string) { return postJSON<SafetyCase>(apiPath(API.safetyDispatch, { case_id: caseId }), {}) }
  safetyRelease(caseId: string, body: SafetyRelease) { return postJSON<SafetyCase>(apiPath(API.safetyRelease, { case_id: caseId }), body) }
  stopReplan(eventId: string) { return postJSON<Event>(apiPath(API.stopReplan, { event_id: eventId }), {}) }
  stopHandoff(eventId: string) { return postJSON<Event>(apiPath(API.stopHandoff, { event_id: eventId }), {}) }
  stopClose(eventId: string, note?: string) { return postJSON<Event>(apiPath(API.stopClose, { event_id: eventId }), note ? { note } : {}) }
  interrupt(eventId: string) { return postJSON<Record<string, unknown>>(`/api/subagents/${encodeURIComponent(eventId)}/interrupt`, {}) }
  async control(c: SimControl) { await postJSON(API.simControl, c) }
  async overlay(scenario: ScenarioName) {
    return (await postJSON<{ overlay: OverlayResult }>(API.simControl, { action: 'overlay', scenario } satisfies SimControl)).overlay
  }
  simEvents() { return getJSON<SimEventsView>(API.simEvents) }
  setSimEvents(b: SimEventsBody) { return postJSON<SimEventsView>(API.simEvents, b) }
  async fireEvent(kind: string) { return (await postJSON<{ ok: boolean; fired: SimEventFired }>(API.simEventsFire, { kind })).fired }
  async clearFault(machineId: string) { await postJSON(API.simClear, { machine_id: machineId }) }
  rollback(version?: number, confirm = false) {
    return postJSON<ScheduleVersion>(API.scheduleRollback, { ...(version == null ? {} : { version }), confirm })
  }
  rollbackPreview(version?: number) {
    return getJSON<RollbackPreview>(`${API.scheduleRollback}/preview${version == null ? '' : `?version=${version}`}`)
  }
  machineDetail(id: string) { return getJSON<MachineDetail>(apiPath(API.machine, { machine_id: id })) }
  scheduleVersions() { return getJSON<ScheduleVersion[]>(API.scheduleVersions) }
  shifts() { return getJSON<Shift[]>(API.shifts) }
  async currentShifts() { return shiftInstances(Date.now(), CURRENT_SHIFTS) }
  capacity() { return getJSON<CapacityReport>(API.capacity) }
  audit() { return getJSON<AuditEntry[]>(`${API.audit}?limit=200`) }
  eventAudit(eventId: string) { return getJSON<AuditEntry[]>(`${API.audit}?event_id=${encodeURIComponent(eventId)}&limit=5000`) }
  async setNetwork(n: Network) { await postJSON('/api/sim/network', { network: n }) }
}

// ---------------- mock ----------------
type BC =
  | { kind: 'hello'; from: string }
  | { kind: 'leader'; from: string; owner?: boolean }
  | { kind: 'ws'; msg: WsMessage }
  | { kind: 'cmd'; id: string; op: string; args: unknown[] }
  | { kind: 'res'; id: string; value: unknown }

/** standalone = lead at once (a ?scenario / ?at page runs its own simulation); channel = BroadcastChannel id suffix:
 *  pages on one channel run one MockEngine (leader election), so the 3D page and its embedded dashboard / phone agree. */
export interface MockOptions { scenario?: ScenarioName; at?: number; speed?: number; standalone?: boolean; channel?: string }

/** This page's private mock channel: ?mockch=<id> joins one (embedded in / linked from a ?scenario page); a ?scenario /
 *  ?at page owns a new one per load, so separate tabs opened with ?scenario stay apart. Otherwise the shared channel. */
const PAGE_Q = new URLSearchParams(location.search)
const MOCK_CHANNEL = PAGE_Q.get('mockch') ?? (PAGE_Q.has('scenario') || PAGE_Q.has('at') ? Math.random().toString(36).slice(2, 10) : undefined)

/** Query string for a page this one embeds or links (the 3D iframes, 「3D 工廠 ↗」): the same params, plus mockch so
 *  that page follows this page's simulation. Live mode ignores mockch (the server keeps every page in sync). */
export function embedSearch(): string {
  if (!MOCK_CHANNEL || PAGE_Q.has('mockch') || PAGE_Q.has('live')) return location.search
  const q = new URLSearchParams(location.search)
  q.set('mockch', MOCK_CHANNEL)
  return `?${q}`
}

export class MockTransport implements Transport {
  mode: Mode = 'mock'
  engine: MockEngine | null = null
  private ch: BroadcastChannel | null = null
  private me = Math.random().toString(36).slice(2)
  private waiters = new Map<string, (v: unknown) => void>()
  private opts: MockOptions
  constructor(opts: MockOptions = {}) { this.opts = opts }

  start(onMsg: (m: WsMessage) => void, onConn: (c: boolean) => void) {
    let leaderTimer: ReturnType<typeof setTimeout> | undefined
    let unsub: (() => void) | null = null
    const becomeLeader = () => {
      this.engine = new MockEngine({ scenario: this.opts.scenario, at: this.opts.at, speed: this.opts.speed })
      unsub = this.engine.subscribe((m) => { onMsg(m); this.ch?.postMessage({ kind: 'ws', msg: m } satisfies BC) })
      this.engine.start()
      onConn(true)
    }
    const hasBC = typeof BroadcastChannel !== 'undefined'
    if (!hasBC) { becomeLeader(); return () => { unsub?.(); this.engine?.stop() } }
    this.ch = new BroadcastChannel(this.opts.channel ? `warroom-mock:${this.opts.channel}` : 'warroom-mock')
    let following = false
    // joined another page's channel (?mockch): if its owner answers late (busy rendering 3D), drop our copy and follow it
    const yields = !!this.opts.channel && !this.opts.standalone
    this.ch.onmessage = async (e: MessageEvent<BC>) => {
      const m = e.data
      if (m.kind === 'hello' && this.engine) this.ch!.postMessage({ kind: 'leader', from: this.me, owner: this.opts.standalone } satisfies BC)
      else if (m.kind === 'leader' && !following && (!this.engine || (yields && m.owner))) {
        if (this.engine) { unsub?.(); this.engine.stop(); this.engine = null }
        following = true; clearTimeout(leaderTimer); onConn(true)
      }
      else if (m.kind === 'ws' && following) onMsg(m.msg)
      else if (m.kind === 'cmd' && this.engine) {
        const value = await this.local(m.op, m.args)
        this.ch!.postMessage({ kind: 'res', id: m.id, value } satisfies BC)
      } else if (m.kind === 'res') { this.waiters.get(m.id)?.(m.value); this.waiters.delete(m.id) }
    }
    // standalone: this page owns its private channel, so it leads at once and answers its embeds' hellos
    if (this.opts.standalone) becomeLeader()
    else {
      this.ch.postMessage({ kind: 'hello', from: this.me } satisfies BC)
      leaderTimer = setTimeout(() => { if (!following) becomeLeader() }, 350)
    }
    return () => { clearTimeout(leaderTimer); unsub?.(); this.engine?.stop(); this.ch?.close() }
  }

  private async local(op: string, args: unknown[]): Promise<unknown> {
    const e = this.engine!
    switch (op) {
      case 'decide': return e.decide(args[0] as ApprovalDecision)
      case 'control': e.control(args[0] as SimControl); return null
      case 'overlay': return e.overlay(args[0] as ScenarioName)
      case 'rollback': return e.rollback(args[0] as number | undefined)
      case 'machineDetail': return e.machineDetail(args[0] as string)
      case 'scheduleVersions': return e.versions()
      case 'shifts': return e.shifts()
      case 'currentShifts': return e.currentShifts()
      case 'capacity': return e.capacity()
      case 'audit': return []
      case 'setNetwork': e.setNetwork(args[0] as Network); return null
      case 'jump': e.jump(args[0] as number); return null
    }
    return null
  }
  private call<T>(op: string, ...args: unknown[]): Promise<T> {
    if (this.engine) return this.local(op, args) as Promise<T>
    if (!this.ch) return Promise.reject(new Error('mock not started'))
    const id = `${this.me}-${Math.random().toString(36).slice(2)}`
    return new Promise<T>((res, rej) => {
      this.waiters.set(id, (v) => res(v as T))
      this.ch!.postMessage({ kind: 'cmd', id, op, args } satisfies BC)
      setTimeout(() => { if (this.waiters.delete(id)) rej(new Error(`mock ${op} timeout`)) }, 3000)
    })
  }
  decide(d: ApprovalDecision) { return this.call<ApprovalRequest | null>('decide', d) }
  control(c: SimControl) { return this.call<void>('control', c) }
  overlay(scenario: ScenarioName) { return this.call<OverlayResult>('overlay', scenario) }
  rollback(v?: number) { return this.call<ScheduleVersion | null>('rollback', v) }
  machineDetail(id: string) { return this.call<MachineDetail>('machineDetail', id) }
  scheduleVersions() { return this.call<ScheduleVersion[]>('scheduleVersions') }
  shifts() { return this.call<Shift[]>('shifts') }
  currentShifts() { return this.call<Shift[]>('currentShifts') }
  capacity() { return this.call<CapacityReport>('capacity') }
  audit() { return this.call<AuditEntry[]>('audit') }
  setNetwork(n: Network) { return this.call<void>('setNetwork', n) }
  jump(t: number) { return this.call<void>('jump', t) }
}

// ---------------- choose ----------------
/**
 * ?mock=1 forces mock, ?live=1 forces live. Otherwise probe /api/health (1.5 s) and fall back to mock.
 * Mock extras: ?scenario=press7_temp_drop&at=02:31:05&speed=10 (at = HH:MM[:SS] on the scenario's date).
 */
export async function pickTransport(): Promise<Transport> {
  const q = new URLSearchParams(location.search)
  const mockOpts = (): MockOptions => {
    const scenario = (q.get('scenario') as ScenarioName | null) ?? undefined
    const speed = q.get('speed') ? Number(q.get('speed')) : undefined
    let at: number | undefined
    const atq = q.get('at')
    if (atq) {
      const date = scenario === 'press7_temp_drop' || scenario === 'concurrent_3' ? '2026-10-03' : '2026-10-22'
      const hhmmss = atq.length === 5 ? `${atq}:00` : atq
      at = Date.parse(`${date}T${hhmmss}+08:00`)
    }
    // ?mockch: follow that page's simulation (and lead it with our own params only if nobody answers)
    return { scenario, speed, at, standalone: !!(scenario || atq) && !q.has('mockch'), channel: MOCK_CHANNEL }
  }
  if (q.has('mock')) return new MockTransport(mockOpts())
  if (q.has('live')) return new LiveTransport()
  try {
    const ctl = new AbortController()
    const t = setTimeout(() => ctl.abort(), 1500)
    const r = await fetch(API.health, { signal: ctl.signal })
    clearTimeout(t)
    const ct = r.headers.get('content-type') ?? ''
    if (r.ok && ct.includes('json')) return new LiveTransport()
  } catch { /* no backend */ }
  return new MockTransport(mockOpts())
}
