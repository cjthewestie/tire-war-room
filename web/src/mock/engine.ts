// Standalone mock of the backend: emits the same WsMessages as /ws and answers the same REST shapes,
// so the dashboard and phone render with no server. Deterministic: state = f(factory time, decisions).
import type {
  ApprovalDecision, ApprovalRequest, CapacityReport, Channel, CompoundStage, DashboardSnapshot, Event, HeaderStatus,
  MachineDetail, MachineState, MachineTile, MetricTile, Prediction, ScenarioName, ScheduleVersion, SensorReading, Shift,
  SimControl, WsMessage,
} from '../contracts'
import {
  CAPACITY_CONDITIONS, CAPACITY_TARGETS, CURRENT_SHIFTS, DEMO_ORDER_PROGRESS, LEAD_TIME_WEEKS, MACHINES, SIZE_LABEL,
} from '../factory'
import { isoTaipei } from '../format'
import { isAbnormal } from '../store'
import { HOUR, MIN, baseJobs, makeSchedule, pressSize, shiftInstances, version } from './schedule'
import { type Ctx, type Script, type ScriptWorld, type Signal, SEC, bmixScript, openMillScript, press7Script, r1 } from './scripts'

/** Tyres already de-molded at the page-11 start (dataset 10/28 02:12 shifted to 10/03): S 21, M 9, L 7 of 100. */
const MOCK_ORDER_DONE: Record<'S' | 'M' | 'L', number> = { S: 21, M: 9, L: 7 }

export type Network = HeaderStatus['network']

interface Base { temp: number; pressure: number | null; flow: number | null; steam: number | null; stage: CompoundStage }
const BASE: Record<string, Base> = {
  'A-MIX': { temp: 108, pressure: 6, flow: 42, steam: null, stage: 'unvulcanized' },
  'A-OPEN': { temp: 88, pressure: 5.5, flow: 40, steam: null, stage: 'unvulcanized' },
  'A-BIGOPEN': { temp: 98, pressure: 5.5, flow: 40, steam: null, stage: 'unvulcanized' },
  'A-SMALLOPEN': { temp: 82, pressure: 5.5, flow: 38, steam: null, stage: 'unvulcanized' },
  'A-CAL': { temp: 78, pressure: 8, flow: 36, steam: null, stage: 'unvulcanized' },
  'A-COOL': { temp: 34, pressure: 2, flow: 55, steam: null, stage: 'unvulcanized' },
  'A-WIND': { temp: 30, pressure: null, flow: null, steam: null, stage: 'unvulcanized' },
  'B-MIX': { temp: 106, pressure: 6, flow: 42, steam: null, stage: 'unvulcanized' },
  'B-OPEN': { temp: 86, pressure: 5.5, flow: 40, steam: null, stage: 'unvulcanized' },
  'B-FORM': { temp: 72, pressure: 150, flow: null, steam: null, stage: 'unvulcanized' },
  'B-ASSY': { temp: 45, pressure: 120, flow: null, steam: null, stage: 'none' },
  'C-PAYOFF': { temp: 27, pressure: null, flow: null, steam: null, stage: 'none' },
  'C-EXTR': { temp: 92, pressure: 9, flow: 30, steam: null, stage: 'unvulcanized' },
  'C-WIND': { temp: 30, pressure: null, flow: null, steam: null, stage: 'none' },
  'C-WRAP': { temp: 40, pressure: 3, flow: null, steam: null, stage: 'none' },
  'D-CUT': { temp: 26, pressure: null, flow: null, steam: null, stage: 'none' },
  'BOND-01': { temp: 62, pressure: 140, flow: null, steam: null, stage: 'unvulcanized' },
}
for (const m of MACHINES) if (m.line === 'CURE') BASE[m.id] = { temp: pressSize(m.id) === 'S' ? 125.5 : 128, pressure: 20, flow: null, steam: 6.5, stage: 'curative_added' }

function hashId(id: string): number { let h = 0; for (const c of id) h = (h * 31 + c.charCodeAt(0)) % 9973; return h }
function baseNoise(id: string, t: number, amp: number): number {
  const h = hashId(id)
  return amp * (0.6 * Math.sin(t / 41_000 + h) + 0.3 * Math.sin(t / 13_000 + 2 * h) + 0.1 * Math.sin(t / 3_700 + 3 * h))
}

export const SCENARIOS: { name: ScenarioName; label: string; start: string; speed: number }[] = [
  { name: 'day_loop', label: '一日循環', start: '2026-10-21T00:00:00+08:00', speed: 60 },
  { name: 'normal', label: '正常運轉', start: '2026-10-22T14:20:00+08:00', speed: 1 },
  { name: 'open_mill_overheat', label: '大開煉機過熱（p.2）', start: '2026-10-22T14:25:30+08:00', speed: 10 },
  { name: 'press7_temp_drop', label: '7 號硫化機溫降（p.11）', start: '2026-10-03T02:12:00+08:00', speed: 10 },
  { name: 'concurrent_3', label: '3 件同時異常', start: '2026-10-03T02:12:00+08:00', speed: 5 },
  { name: 'bad_data', label: '壞資料（不喚醒 AI）', start: '2026-10-22T09:00:00+08:00', speed: 10 },
  // issue #17 plant events (backend only; mock mode plays them as normal running)
  { name: 'shift_start', label: '早班開工', start: '2026-10-05T07:50:00+08:00', speed: 10 },
  { name: 'rush_order', label: '插單', start: '2026-10-07T09:50:00+08:00', speed: 10 },
  { name: 'staffing', label: '人力調度', start: '2026-10-06T07:20:00+08:00', speed: 10 },
]

interface LoadedScenario { name: ScenarioName; scripts: Script[]; press7?: ReturnType<typeof press7Script>; badData?: string }

function buildScenario(name: ScenarioName): LoadedScenario {
  const T = (iso: string) => Date.parse(iso)
  switch (name) {
    case 'open_mill_overheat': return { name, scripts: [openMillScript(T('2026-10-22T14:13:00+08:00'))] }
    case 'press7_temp_drop': { const p = press7Script(T('2026-10-03T02:14:00+08:00')); return { name, scripts: [p], press7: p } }
    case 'concurrent_3': {
      const p = press7Script(T('2026-10-03T02:14:00+08:00'))
      return { name, scripts: [p, openMillScript(T('2026-10-03T02:14:30+08:00')), bmixScript(T('2026-10-03T02:15:00+08:00'))], press7: p }
    }
    case 'bad_data': return { name, scripts: [], badData: 'CURE-04' }
    default: return { name, scripts: [] }
  }
}

export interface EngineOptions { scenario?: ScenarioName; at?: number; speed?: number }

export class MockEngine {
  scenario!: LoadedScenario
  now = 0
  speed = 1
  paused = false
  network: Network = 'online'
  private decisions = new Map<string, { d: ApprovalDecision; at: number }>()
  private rollbacks: { at: number; to: number }[] = []
  private listeners = new Set<(m: WsMessage) => void>()
  private timer: ReturnType<typeof setInterval> | null = null
  private prevPending = new Set<string>()
  private prevResolved = new Set<string>()
  private prevVersions = 0
  private prevEventStates = new Map<string, string>()
  private cache: { now: number; rev: number; world: ScriptWorld[] } | null = null
  private rev = 0
  private ctx: Ctx = { userDecision: (id) => this.decisions.get(id) }

  constructor(opts: EngineOptions = {}) { this.load(opts.scenario ?? 'open_mill_overheat', opts.at, opts.speed) }

  load(name: ScenarioName, at?: number, speed?: number) {
    const def = SCENARIOS.find((s) => s.name === name) ?? SCENARIOS[0]
    this.scenario = buildScenario(def.name)
    this.now = at ?? Date.parse(def.start)
    this.speed = speed ?? def.speed
    this.decisions.clear()
    this.rollbacks = []
    this.prevPending.clear(); this.prevResolved.clear(); this.prevVersions = 0; this.prevEventStates.clear()
    this.rev++
  }

  subscribe(fn: (m: WsMessage) => void): () => void { this.listeners.add(fn); return () => this.listeners.delete(fn) }
  private emit(m: WsMessage) { for (const l of this.listeners) l(m) }
  private msg<T extends WsMessage['type']>(type: T, data: Extract<WsMessage, { type: T }>['data']): WsMessage {
    return { type, ts: isoTaipei(this.now), data } as WsMessage
  }

  start(tickMs = 250) {
    if (this.timer) return
    this.publish(true)
    this.timer = setInterval(() => {
      if (!this.paused) this.now += tickMs * this.speed
      this.publish(false)
    }, tickMs)
  }
  stop() { if (this.timer) clearInterval(this.timer); this.timer = null }

  // ---------------- world ----------------
  private worlds(): ScriptWorld[] {
    if (this.cache && this.cache.now === this.now && this.cache.rev === this.rev) return this.cache.world
    const world = this.scenario.scripts.map((s) => s.world(this.now, this.ctx))
    this.cache = { now: this.now, rev: this.rev, world }
    return world
  }

  signal(id: string, t: number): { temp_c: number; pressure_kgf_cm2: number | null; water_flow_lpm: number | null; steam_kgf_cm2: number | null } {
    const b = BASE[id]
    let o: Signal = {}
    for (const s of this.scenario.scripts) { const v = s.signal(id, t, this.ctx); if (v) o = { ...o, ...v } }
    const temp = o.temp_c ?? b.temp + baseNoise(id, t, b.temp > 100 ? 0.6 : 0.4)
    let tempOut = temp
    if (this.scenario.badData === id && t >= Date.parse('2026-10-22T09:00:30+08:00')) {
      const k = Math.floor(t / 10_000) % 3
      tempOut = k === 0 ? 182.0 : k === 1 ? temp : -5   // jumps → validate() flags, never wakes AI
    }
    return {
      temp_c: r1(tempOut),
      pressure_kgf_cm2: b.pressure == null ? null : r1(o.pressure_kgf_cm2 ?? b.pressure + baseNoise(id + 'p', t, b.pressure * 0.01)),
      water_flow_lpm: b.flow == null ? null : r1(o.water_flow_lpm ?? b.flow + baseNoise(id + 'f', t, 0.5)),
      steam_kgf_cm2: b.steam == null ? null : Math.round((o.steam_kgf_cm2 ?? b.steam + baseNoise(id + 's', t, 0.05)) * 100) / 100,
    }
  }

  private reading(id: string, t: number, seq: number): SensorReading {
    const s = this.signal(id, t)
    let quality: SensorReading['quality'] = 'ok'
    if (this.scenario.badData === id && (s.temp_c > 160 || s.temp_c < 0)) quality = s.temp_c < 0 ? 'out_of_range' : 'jump'
    return { ts: isoTaipei(t), machine_id: id, ...s, seq, quality }
  }

  versions(): ScheduleVersion[] {
    let vs: ScheduleVersion[]
    if (this.scenario.press7) vs = this.scenario.press7.versions(this.now, this.ctx)
    else {
      const anchor = Math.floor(this.now / (24 * HOUR)) * 24 * HOUR - 8 * HOUR
      vs = [version(1, null, 'active', anchor - 24 * HOUR, '週排程（硫化機為中心，出模排進有人時段）', null, makeSchedule('SCH-v1', anchor - 24 * HOUR, baseJobs(anchor + 6 * HOUR)))]
    }
    vs = vs.map((v) => ({ ...v }))
    for (const rb of this.rollbacks) {
      if (rb.at > this.now) continue
      const cur = vs.find((v) => v.status === 'active')
      const target = vs.find((v) => v.version === rb.to)
      if (cur && target && cur !== target) { cur.status = 'rolled_back'; target.status = 'active' }
    }
    return vs
  }

  machineStates(): MachineState[] {
    const worlds = this.worlds()
    const active = this.versions().find((v) => v.status === 'active')
    const patches: ScriptWorld['machine'] = {}
    for (const w of worlds) Object.assign(patches, w.machine)
    return MACHINES.map((m) => {
      const r = this.reading(m.id, this.now, Math.floor(this.now / SEC))
      const p = patches[m.id] ?? {}
      let cure_job: MachineState['cure_job'] = null
      if (m.line === 'CURE' && active) {
        const j = active.schedule.jobs.find((x) => x.press_id === m.id && Date.parse(x.mold_in) <= this.now && Date.parse(x.demold) > this.now)
        if (j) cure_job = {
          job_id: j.id, order_id: j.order_id, size: j.size, mold_id: j.mold_id ?? '', mold_in_at: j.mold_in, planned_demold_at: j.demold,
          setpoint_c: pressSize(m.id) === 'S' ? 125 : 128, cure_required_h: j.cure_h, cure_elapsed_h: r1((this.now - Date.parse(j.mold_in)) / HOUR),
        }
      }
      let status = p.status ?? 'running'
      let note = p.note ?? null
      if (m.line === 'CURE' && !cure_job && !p.status) status = 'idle'
      if (this.scenario.badData === m.id && r.quality !== 'ok') { status = 'warning'; note = '資料異常（跳值）→ 驗證層攔下，不喚醒 AI' }
      return {
        machine_id: m.id, line: m.line, name: m.name, short_name: m.short, status, last_reading: r,
        step_elapsed_min: Math.floor((this.now / MIN + hashId(m.id)) % 45), compound_stage: BASE[m.id].stage,
        batch_id: m.line === 'CURE' ? null : `B-${hashId(m.id) % 900 + 100}`, cure_job, active_event_id: p.active_event_id ?? null, note,
      }
    })
  }

  private approvals(): ApprovalRequest[] {
    return this.worlds().flatMap((w) => w.approvals)
  }

  snapshot(): DashboardSnapshot {
    const worlds = this.worlds()
    const states = this.machineStates()
    const tiles: MachineTile[] = states.map((s) => ({ machine_id: s.machine_id, line: s.line, short_name: s.short_name, status: s.status, temp_c: s.last_reading?.temp_c ?? null }))
    const abnormal = tiles.filter((t) => isAbnormal(t.status)).length
    const events = worlds.map((w) => w.event).filter((e): e is Event => !!e)
    const focusW = worlds.filter((w) => w.focus && w.event).sort((a, b) => {
      const ao = a.event!.state === 'closed' ? 1 : 0, bo = b.event!.state === 'closed' ? 1 : 0
      return ao - bo || (a.event!.opened_at < b.event!.opened_at ? -1 : 1)
    })[0]
    const bigOpenAlarm = states.find((s) => s.machine_id === 'A-BIGOPEN')!.status === 'alarm'
    const midnight = Date.parse(isoTaipei(this.now).slice(0, 10) + 'T17:00:00+08:00')
    return {
      header: { normal_count: tiles.length - abnormal, abnormal_count: abnormal, factory_time: isoTaipei(this.now), llm_mode: 'mock', network: this.network },
      orders: (['S', 'M', 'L'] as const).map((sz) => {
        // the team dataset's tyres de-molded by 02:12 (issue #20), the page-2 hours scaled by tyres left (as Factory.order_progress)
        const [d0, total, hours] = DEMO_ORDER_PROGRESS[sz]
        const done = MOCK_ORDER_DONE[sz]
        return { size: sz, label: SIZE_LABEL[sz], done, total, remaining_h: Math.round((hours * (total - done) / (total - d0)) * 10) / 10 }
      }),
      semi_finished: {
        tire_label: '大胎 #07', order_id: '#L-307', size: 'L', planned_mold_in_at: isoTaipei(midnight),
        parts: [
          { part: 'A', label: 'A 胎體／胎面膠', status_text: '預熱中', state: bigOpenAlarm ? 'alert' : 'in_progress', machine_id: 'A-BIGOPEN' },
          { part: 'B', label: 'B 胎側膠', status_text: '成形中', state: 'in_progress', machine_id: 'B-FORM' },
          { part: 'C', label: 'C 鋼絲圈', status_text: '包布中', state: 'in_progress', machine_id: 'C-WRAP' },
          { part: 'D', label: 'D 尼龍布', status_text: '已完成', state: 'done', machine_id: 'D-CUT' },
        ],
      },
      focus: focusW?.focus ?? null,
      machines: tiles,
      events,
      pending_approvals: this.approvals().filter((a) => a.status === 'pending'),
      safety_actions: worlds.flatMap((w) => w.safety),
    }
  }

  private publish(full: boolean) {
    const snap = this.snapshot()
    this.emit(this.msg('clock', { factory_time: isoTaipei(this.now), speed: this.speed, paused: this.paused }))
    this.emit(this.msg('snapshot', snap))
    const all = this.approvals()
    for (const a of all) {
      if (a.status === 'pending' && !this.prevPending.has(a.id)) { this.prevPending.add(a.id); this.emit(this.msg('approval_request', a)) }
      if (a.status !== 'pending' && !this.prevResolved.has(a.id)) { this.prevResolved.add(a.id); this.prevPending.delete(a.id); this.emit(this.msg('approval_resolved', a)) }
    }
    for (const e of snap.events) {
      if (this.prevEventStates.get(e.id) !== e.state) {
        if (!this.prevEventStates.has(e.id) && e.alert_card) this.emit(this.msg('alert_card', { event_id: e.id, card: e.alert_card }))
        this.prevEventStates.set(e.id, e.state)
        this.emit(this.msg('event', e))
      }
    }
    const vs = this.versions()
    if (full || vs.length !== this.prevVersions) {
      this.prevVersions = vs.length
      const cur = vs.find((v) => v.status === 'active') ?? vs[vs.length - 1]
      this.emit(this.msg('schedule', cur))
    }
  }

  // ---------------- commands ----------------
  decide(d: ApprovalDecision): ApprovalRequest | null {
    const a = this.approvals().find((x) => x.id === d.request_id)
    if (!a || a.status !== 'pending') return a ?? null
    this.decisions.set(d.request_id, { d: { ...d, decided_at: isoTaipei(this.now) }, at: this.now })
    this.rev++
    this.publish(false)
    return this.approvals().find((x) => x.id === d.request_id) ?? null
  }

  control(c: SimControl) {
    if (c.action === 'play') this.paused = false
    if (c.action === 'pause') this.paused = true
    if (c.action === 'speed' && c.speed) this.speed = c.speed
    if (c.action === 'reset') this.load(this.scenario.name)
    if (c.action === 'scenario' && c.scenario) this.load(c.scenario)
    this.publish(true)
  }

  jump(t: number) { this.now = t; this.rev++; this.publish(true) }

  setNetwork(n: Network) { this.network = n; this.publish(false) }

  rollback(to?: number): ScheduleVersion | null {
    const vs = this.versions()
    const cur = vs.find((v) => v.status === 'active')
    const target = to ?? cur?.parent_version ?? null
    if (target == null) return null
    this.rollbacks.push({ at: this.now, to: target })
    this.rev++
    this.publish(true)
    return this.versions().find((v) => v.version === target) ?? null
  }

  machineDetail(id: string): MachineDetail {
    const state = this.machineStates().find((s) => s.machine_id === id)!
    const history: SensorReading[] = []
    for (let k = 180; k >= 0; k--) { const t = this.now - k * 10 * SEC; history.push(this.reading(id, t, Math.floor(t / SEC))) }
    const r = state.last_reading!
    const b = BASE[id]
    const tiles: MetricTile[] = [
      { label: '溫度', value: `${r.temp_c}°C`, status: tileStatus(id, 'temp_c', r.temp_c, b) },
      { label: '壓力', value: r.pressure_kgf_cm2 == null ? '—' : `${r.pressure_kgf_cm2} kgf/cm²`, status: 'normal' },
      { label: '時間', value: state.cure_job ? `${state.cure_job.cure_elapsed_h} / ${state.cure_job.cure_required_h} h` : `${state.step_elapsed_min} 分`, status: 'normal' },
      { label: '水流', value: r.water_flow_lpm == null ? '—' : `${r.water_flow_lpm} L/min`, status: tileStatus(id, 'water_flow_lpm', r.water_flow_lpm, b) },
    ]
    if (r.steam_kgf_cm2 != null) tiles.push({ label: '蒸汽', value: `${r.steam_kgf_cm2} kgf/cm²`, status: r.steam_kgf_cm2 < 6 ? 'alarm' : 'normal' })
    const last = history.slice(-30).filter((h) => h.quality === 'ok')
    let prediction: Prediction | null = null
    if (last.length > 2) {
      const slope = ((last[last.length - 1].temp_c ?? 0) - (last[0].temp_c ?? 0)) / ((last.length - 1) * 10 / 60)
      const nowV = last[last.length - 1].temp_c ?? 0
      const pred = r1(nowV + slope * 30 * 0.6)
      prediction = { machine_id: id, channel: 'temp_c', now_value: nowV, predicted_value: pred, band_lo: r1(pred - 1), band_hi: r1(pred + 1), horizon_s: 1800, trend_per_min: Math.round(slope * 100) / 100, model: 'mock-linear' }
    }
    return { state, tiles, history, prediction }
  }

  shifts(): Shift[] {
    const req: Record<string, number> = {}
    if (this.scenario.press7) req[`${isoTaipei(this.now).slice(0, 10)}|body|夜班`] = 3   // page 11: night shift short 1
    return shiftInstances(this.now, undefined, req)
  }
  currentShifts(): Shift[] { return shiftInstances(this.now, CURRENT_SHIFTS) }

  capacity(): CapacityReport {
    return {
      rows: CAPACITY_TARGETS.map((r) => ({ ...r })), ml_100_weeks: LEAD_TIME_WEEKS.ml, small_100_weeks: LEAD_TIME_WEEKS.small,
      conditions: CAPACITY_CONDITIONS,
    }
  }
}

function tileStatus(id: string, ch: Channel, v: number | null, b: Base): MetricTile['status'] {
  if (v == null) return 'normal'
  if (ch === 'temp_c') {
    if (id.startsWith('CURE')) return v < 120 || v > 130 ? 'alarm' : v < 123 ? 'warn' : 'normal'
    if (b.stage === 'unvulcanized' && v >= 110) return 'alarm'
    return Math.abs(v - b.temp) > 5 ? 'warn' : 'normal'
  }
  if (ch === 'water_flow_lpm' && b.flow) return v < b.flow * 0.75 ? 'alarm' : 'normal'
  return 'normal'
}
