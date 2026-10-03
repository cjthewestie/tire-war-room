// One interface, two backends: the real FastAPI (REST + /ws) or the in-browser MockEngine.
// Mock mode syncs tabs over BroadcastChannel, so approving on /phone updates the dashboard.
import {
  API, apiPath,
  type ApprovalDecision, type ApprovalRequest, type AuditEntry, type CapacityReport, type DashboardSnapshot,
  type MachineDetail, type ScenarioName, type ScheduleVersion, type Shift, type SimControl, type WsMessage,
} from './contracts'
import { MockEngine, type Network } from './mock/engine'
import { CURRENT_SHIFTS } from './factory'
import { shiftInstances } from './mock/schedule'

export type Mode = 'live' | 'mock'

export interface Transport {
  mode: Mode
  start(onMsg: (m: WsMessage) => void, onConn: (c: boolean) => void): () => void
  decide(d: ApprovalDecision): Promise<ApprovalRequest | null>
  control(c: SimControl): Promise<void>
  rollback(version?: number): Promise<ScheduleVersion | null>
  machineDetail(id: string): Promise<MachineDetail>
  scheduleVersions(): Promise<ScheduleVersion[]>
  shifts(): Promise<Shift[]>
  currentShifts(): Promise<Shift[]>
  capacity(): Promise<CapacityReport>
  audit(): Promise<AuditEntry[]>
  /** Mock only: simulate the offline tiers (page 9). Live: no-op, the backend reports `header.network`. */
  setNetwork(n: Network): Promise<void>
  /** Mock only: jump factory time. */
  jump?(t: number): Promise<void>
}

// ---------------- live ----------------
async function getJSON<T>(path: string): Promise<T> {
  const r = await fetch(path)
  if (!r.ok) throw new Error(`${r.status} ${path}`)
  return (await r.json()) as T
}
async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  if (!r.ok) throw new Error(`${r.status} ${path}`)
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
    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      ws = new WebSocket(`${proto}://${location.host}${API.ws}`)
      ws.onopen = () => { onConn(true); retry = 0; pullSnapshot() }
      ws.onclose = () => {
        onConn(false)
        if (!closed) timer = setTimeout(connect, Math.min(5000, 500 * 2 ** retry++))
      }
      ws.onmessage = (e) => { try { onMsg(JSON.parse(e.data) as WsMessage) } catch { /* ignore malformed */ } }
    }
    pullSnapshot()
    getJSON<ScheduleVersion>(API.schedule).then((v) => onMsg({ type: 'schedule', ts: v.created_at, data: v })).catch(() => {})
    connect()
    // safety net: focus panel and counts come from snapshots; refresh in case the server only sends deltas
    const poll = setInterval(pullSnapshot, 4000)
    return () => { closed = true; clearTimeout(timer); clearInterval(poll); ws?.close() }
  }
  decide(d: ApprovalDecision) { return postJSON<ApprovalRequest>(apiPath(API.approvalDecision, { request_id: d.request_id }), d) }
  async control(c: SimControl) { await postJSON(API.simControl, c) }
  rollback(version?: number) { return postJSON<ScheduleVersion>(API.scheduleRollback, version == null ? {} : { version }) }
  machineDetail(id: string) { return getJSON<MachineDetail>(apiPath(API.machine, { machine_id: id })) }
  scheduleVersions() { return getJSON<ScheduleVersion[]>(API.scheduleVersions) }
  shifts() { return getJSON<Shift[]>(API.shifts) }
  async currentShifts() { return shiftInstances(Date.now(), CURRENT_SHIFTS) }
  capacity() { return getJSON<CapacityReport>(API.capacity) }
  audit() { return getJSON<AuditEntry[]>(`${API.audit}?limit=200`) }
  async setNetwork() { /* reported by the backend */ }
}

// ---------------- mock ----------------
type BC =
  | { kind: 'hello'; from: string }
  | { kind: 'leader'; from: string }
  | { kind: 'ws'; msg: WsMessage }
  | { kind: 'cmd'; id: string; op: string; args: unknown[] }
  | { kind: 'res'; id: string; value: unknown }

export interface MockOptions { scenario?: ScenarioName; at?: number; speed?: number; standalone?: boolean }

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
    if (!hasBC || this.opts.standalone) { becomeLeader(); return () => { unsub?.(); this.engine?.stop() } }
    this.ch = new BroadcastChannel('warroom-mock')
    let following = false
    this.ch.onmessage = async (e: MessageEvent<BC>) => {
      const m = e.data
      if (m.kind === 'hello' && this.engine) this.ch!.postMessage({ kind: 'leader', from: this.me } satisfies BC)
      else if (m.kind === 'leader' && !this.engine && !following) { following = true; clearTimeout(leaderTimer); onConn(true) }
      else if (m.kind === 'ws' && following) onMsg(m.msg)
      else if (m.kind === 'cmd' && this.engine) {
        const value = await this.local(m.op, m.args)
        this.ch!.postMessage({ kind: 'res', id: m.id, value } satisfies BC)
      } else if (m.kind === 'res') { this.waiters.get(m.id)?.(m.value); this.waiters.delete(m.id) }
    }
    this.ch.postMessage({ kind: 'hello', from: this.me } satisfies BC)
    leaderTimer = setTimeout(() => { if (!following) becomeLeader() }, 350)
    return () => { clearTimeout(leaderTimer); unsub?.(); this.engine?.stop(); this.ch?.close() }
  }

  private async local(op: string, args: unknown[]): Promise<unknown> {
    const e = this.engine!
    switch (op) {
      case 'decide': return e.decide(args[0] as ApprovalDecision)
      case 'control': e.control(args[0] as SimControl); return null
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
    return { scenario, speed, at, standalone: !!(scenario || atq) }
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
