// Scripted demo scenarios for the standalone mock mode (pages 2 and 11 of the proposal).
// Everything is a pure function of factory time + supervisor decisions, so history, replay and
// "jump to 02:31:05" are deterministic. Physics numbers follow the team dataset's EV-2 (small tire S-027: 28.5 h, 119.5 °C, …).
import type {
  ApprovalDecision, ApprovalOption, ApprovalRequest, Channel, Event, EventState, FocusPanel, GateDecision,
  Hypothesis, MachineStatus, PlanStep, RiskLevel, SafetyAction, ScheduleVersion, StepRun, TimelineItem, ToolName,
  ToolResult,
} from '../contracts'
import { machineLabel } from '../factory'
import { isoTaipei } from '../format'
import { HOUR, MIN, baseJobs, makeJob, makeSchedule, reflow, version } from './schedule'

export const SEC = 1000
export type Signal = Partial<Record<Channel, number>>

export interface Resolved { d: ApprovalDecision; at: number; auto: boolean }
export interface Ctx {
  userDecision(id: string): { d: ApprovalDecision; at: number } | undefined
}
export interface ScriptWorld {
  event: Event | null
  approvals: ApprovalRequest[]
  safety: SafetyAction[]
  focus: FocusPanel | null
  machine: Record<string, { status?: MachineStatus; note?: string | null; active_event_id?: string | null }>
}
export interface Script {
  id: string
  machine: string
  signal(id: string, t: number, ctx: Ctx): Signal | null
  world(now: number, ctx: Ctx): ScriptWorld
}

// ---------- helpers ----------
const lerp = (a: number, b: number, f: number) => a + (b - a) * Math.max(0, Math.min(1, f))
export const r1 = (x: number) => Math.round(x * 10) / 10

function resolve(ctx: Ctx, id: string, created: number, now: number, deadline: number | null, def: Omit<ApprovalDecision, 'request_id'>): Resolved | null {
  const u = ctx.userDecision(id)
  if (u && u.at >= created && u.at <= now && (deadline == null || u.at <= deadline)) return { d: u.d, at: u.at, auto: false }
  if (deadline != null && now >= deadline) {
    return { d: { request_id: id, ...def, decided_at: isoTaipei(deadline) }, at: deadline, auto: true }
  }
  return null
}

function stateAt(now: number, marks: [number, EventState][]): EventState {
  let s = marks[0][1]
  for (const [t, st] of marks) if (now >= t) s = st
  return s
}

function result(tool: ToolName, data: Record<string, unknown>, ms = 40): ToolResult {
  return { call_id: `call-${tool}-${Math.round(ms)}`, tool, ok: true, data, error: null, duration_ms: ms, attempt: 1 }
}
function gate(step: string, tool: ToolName, risk: RiskLevel, verdict: GateDecision['verdict'] = 'needs_approval'): GateDecision {
  return {
    step_id: step, tool, verdict, risk,
    checks: { allow_list: true, state_allowed: true, format_ok: true, simulation_feasible: true },
    reasons: verdict === 'allow' ? [`${risk} 自動放行`] : [`${risk} 需主管核准`],
  }
}
function step(id: string, tool: ToolName, args: Record<string, unknown>, deps: string[], purpose: string): PlanStep {
  return { id, tool, args, depends_on: deps, purpose }
}
function run(s: PlanStep, state: StepRun['state'], res: ToolResult | null = null, g: GateDecision | null = null): StepRun {
  return { step: s, state, result: res, gate: g }
}
function approval(o: {
  id: string; event: string; step: string; round: number; created: number; risk: RiskLevel; title: string; reason: string
  done: string[]; gaps: string[]; impact: string; options: ApprovalOption[]; res: Resolved | null
}): ApprovalRequest {
  const status: ApprovalRequest['status'] = !o.res ? 'pending'
    : o.res.d.decision === 'approve' ? 'approved' : o.res.d.decision === 'modify' ? 'modified' : 'rejected'
  return {
    id: o.id, event_id: o.event, step_id: o.step, round: o.round, created_at: isoTaipei(o.created), risk: o.risk,
    title: o.title, reason: o.reason, done: o.done, gaps: o.gaps, delivery_impact: o.impact, options: o.options,
    status, decision: o.res ? { ...o.res.d, decided_at: isoTaipei(o.res.at) } : null, expires_at: null,
  }
}
function tl(t: number, actor: TimelineItem['actor'], text: string, tag: TimelineItem['tag']): TimelineItem {
  return { ts: isoTaipei(t), actor, text, tag }
}
function noise(id: string, t: number, amp = 0.15): number {
  let h = 0
  for (const c of id) h = (h * 31 + c.charCodeAt(0)) % 997
  return amp * (Math.sin(t / 23_000 + h) + 0.6 * Math.sin(t / 7_100 + 2 * h))
}

// =====================================================================================
// Page 11 — 硫化機 7 號 128 → 122 °C: extend cure, verify fails, swap hypothesis
// =====================================================================================
export function press7Script(A: number, single = false): Script & { versions(now: number, ctx: Ctx): ScheduleVersion[] } {
  const EV = `EV-${isoTaipei(A).slice(0, 10).replace(/-/g, '')}-${isoTaipei(A).slice(11, 19).replace(/:/g, '')}-CURE-07`
  const at = (s: number) => A + s * SEC
  // single = 自動播放 Demo（CURE-07 單一事件、單輪核准、128 → 122.3°C）：時間表壓縮，且不走 H1 → H2 第二輪
  const K = single ? { hyp: 2, plan: 5, sim: 8, gate: 13, card: 15 } : { hyp: 3, plan: 9, sim: 11, gate: 25, card: 26 }
  const R1_CREATED = at(K.card), R1_DEADLINE = single ? null : at(160)          // 02:16:40
  const VERIFY = at(1020), H2_T = at(1024), SAFETY_T = at(877) // 02:31:00, 02:31:04, 02:28:37
  const R2_CREATED = at(1032), R2_DEADLINE = at(1050)       // 02:31:30
  const moldIn07 = A - 14.4 * HOUR                           // 14.4 h into a 24 h small cure (dataset EV-2, S-027)
  const NIGHT = isoTaipei(A).slice(5, 10).replace('-', '/')  // "10/03"

  const temp07 = (t: number) => {
    const rel = (t - A) / SEC
    let v: number
    if (single) v = rel < -240 ? 128 : rel < 0 ? lerp(128, 122.3, (rel + 240) / 240) : lerp(122.3, 122, rel / 1800)
    else if (rel < -240) v = 128
    else if (rel < 0) v = lerp(128, 122, (rel + 240) / 240)
    else if (rel < 360) v = lerp(122, 121.8, rel / 360)
    else if (rel < 1020) v = lerp(121.8, 119.5, (rel - 360) / 660)
    else v = 119.5 - 4 * (1 - Math.exp(-(rel - 1020) / 1200))
    return v + noise('CURE-07', t, rel > -240 && rel < 1100 ? 0.05 : 0.15)
  }

  const r1Res = (now: number, ctx: Ctx) => resolve(ctx, 'APR-P7-1', R1_CREATED, now, R1_DEADLINE,
    { decision: 'approve', option_id: 'A', reason: '動作把關確認可行', decided_by: '主管（示範腳本）', via: 'auto' })
  const r2Res = (now: number, ctx: Ctx) => resolve(ctx, 'APR-P7-2', R2_CREATED, now, R2_DEADLINE,
    { decision: 'approve', option_id: 'A', reason: '否決跨組調人，改選夜班加班', decided_by: '主管（示範腳本）', via: 'auto' })

  const H1: Hypothesis = {
    id: 'H1', statement: '蒸汽壓不足，溫度會穩定', cause_code: 'steam_pressure_low', status: 'active',
    expectation: { machine_id: 'CURE-07', channel: 'temp_c', op: '>=', value: 121, value_hi: null, within_min: 30 },
    evidence: ['30 分鐘溫度 128 → 122°C', '蒸汽壓 6.5 → 5.2 kgf/cm²', 'SOP-CP-02'],
  }
  const H2: Hypothesis = {
    id: 'H2', statement: '持續洩壓或感測器漂移', cause_code: 'leak_or_sensor_drift', status: 'active', expectation: null,
    evidence: ['驗證：溫度續降至 119.5°C', '同管線 6、8 號蒸汽與溫度正常', 'SOP「硫化中斷處置」'],
  }

  function versions(now: number, ctx: Ctx): ScheduleVersion[] {
    const anchors = { 'CURE-07': { moldIn: moldIn07, orderId: '#S-307', moldId: 'S-307' } }
    const next = { 'CURE-07': { orderId: '#A-311', moldId: 'A-311' } }
    const base = baseJobs(A, anchors, next)
    const out: ScheduleVersion[] = []
    const v1 = makeSchedule('SCH-v1', A - 2 * 24 * HOUR, base)
    out.push(version(1, null, 'active', A - 2 * 24 * HOUR, '週排程（硫化機為中心，出模排進有人時段）', null, v1))
    if (now < at(K.gate)) return out
    // v2: CURE-07 cure extended to 28.5 h (small tire, dataset EV-2); #A-311 (small) still fits on press 7;
    //     night shift takes 1 from line 2
    let j2 = base.map((j) => ({ ...j }))
    const i07 = j2.findIndex((j) => j.press_id === 'CURE-07' && j.order_id === '#S-307')
    j2[i07] = makeJob('CURE-07', 'S', moldIn07, 28.5, '#S-307', 'S-307', false)
    j2[i07].id = base.find((j) => j.order_id === '#S-307')!.id
    j2[i07].frozen = true
    j2 = reflow(j2, 'CURE-07', Date.parse(j2[i07].demold) + 30 * MIN)
    const transfer = { kind: 'transfer' as const, shift_date: isoTaipei(A).slice(0, 10), shift_name: '夜班', to_team: 'body' as const, from_team: 'side' as const, from_label: '2 線', people: 1, label: `${NIGHT} 夜班從 2 線調 1 人` }
    const v2s = makeSchedule('SCH-v2', at(K.gate), j2, [transfer], ['7 號硫化時間延長至 28.5 h（+4.5 h）'])
    const res1 = r1Res(now, ctx)
    const v2active = !!res1 && res1.d.decision !== 'reject'
    out[0].status = v2active ? 'superseded' : 'active'
    out.push(version(2, 1, v2active ? 'active' : res1 ? 'superseded' : 'proposed', at(K.gate), 'S3 求解：#A-311 維持 7 號機、夜班從 2 線調 1 人', EV, v2s))
    if (single || now < at(1031)) return out
    // v3: CURE-07 out of service; overtime (A) or transfer (B)
    const res2 = r2Res(now, ctx)
    const optB = res2?.d.option_id === 'B'
    const a311 = j2.find((j) => j.order_id === '#A-311')!
    let j3 = j2.filter((j) => !(j.press_id === 'CURE-07' && Date.parse(j.mold_in) > A))
    const cur04 = j3.filter((j) => j.press_id === 'CURE-04' && Date.parse(j.mold_in) <= A && Date.parse(j.demold) > A)[0]
    const slot04 = cur04 ? Date.parse(cur04.demold) + 30 * MIN : A + HOUR
    const moved = makeJob('CURE-04', 'S', slot04, 24, '#A-311', 'A-311', true)
    moved.id = a311.id
    const after04 = isoTaipei(Date.parse(moved.demold) + 30 * MIN)
    j3 = j3.map((j) => (j.press_id === 'CURE-04' && Date.parse(j.mold_in) >= slot04 - 1 ? { ...j, mold_in: after04 } : j))
    j3.push(moved)
    j3 = reflow(j3, 'CURE-04', Date.parse(after04))
    const staffing = optB ? [transfer] : [{ kind: 'overtime' as const, shift_date: transfer.shift_date, shift_name: '夜班', to_team: 'body' as const, from_team: null, from_label: null, people: 1, label: `${NIGHT} 夜班加班 1 人` }]
    const v3s = makeSchedule('SCH-v3', at(1031), j3, staffing, ['7 號機停用（硫化中斷處置）；#A-311 改排 4 號機，後續小胎模次由其他小胎機與週末班吸收'])
    const v3active = !!res2 && res2.d.decision !== 'reject'
    if (v3active) out.forEach((v) => { if (v.status === 'active') v.status = 'superseded' })
    out.push(version(3, out[out.length - 1].version, v3active ? 'active' : res2 ? 'superseded' : 'proposed', at(1031), '7 號停用重排：' + (optB ? '從 2 線調 1 人' : '夜班加班'), EV, v3s))
    return out
  }

  function world(now: number, ctx: Ctx): ScriptWorld {
    const empty: ScriptWorld = { event: null, approvals: [], safety: [], focus: null, machine: {} }
    if (now < A) return empty
    const res1 = r1Res(now, ctx)
    const res2 = r2Res(now, ctx)
    const d1 = res1?.at ?? Infinity
    const d2 = res2?.at ?? Infinity
    const marksFull: [number, EventState][] = [
      [A, 'detected'], [at(1), 'classify'], [at(K.plan), 'plan'], [at(K.sim), 'simulate'], [at(K.gate), 'gate'], [R1_CREATED, 'await_approval'],
      [d1, 'execute'], [d1 + 2 * SEC, 'verify'], [VERIFY, 'replan'], [at(1030), 'simulate'], [at(1031), 'gate'], [R2_CREATED, 'await_approval'],
      [d2, 'execute'], [d2 + 3 * SEC, 'verify'], [d2 + 10 * SEC, 'closed'],
    ]
    const closeT = single ? d1 + 70 * SEC : d2 + 10 * SEC
    const marks: [number, EventState][] = single
      ? [[A, 'detected'], [at(1), 'classify'], [at(K.plan), 'plan'], [at(K.sim), 'simulate'], [at(K.gate), 'gate'], [R1_CREATED, 'await_approval'],
         [d1, 'execute'], [d1 + 30 * SEC, 'verify'], [closeT, 'closed']]
      : marksFull
    const state = stateAt(now, marks)
    const round2 = !single && now >= VERIFY

    // ---- timeline (page 11 table) ----
    const T: TimelineItem[] = []
    const push = (t: number, a: TimelineItem['actor'], text: string, tag: TimelineItem['tag']) => { if (now >= t) T.push(tl(t, a, text, tag)) }
    push(A, '事件偵測', single ? '[工具] 7 號 128 → 122.3°C，預測穩定在 122±1°C → 喚醒 AI' : '[工具] 7 號 128 → 122°C，預測穩定在 122±1°C → 喚醒指揮官', 'tool')
    push(at(1), '安全核心', '仍在 120–130°C，不停機（低於 120°C 直接處置，不等 AI）', 'safety')
    push(at(K.hyp), '指揮官', single ? '[工具] 讀 30 分鐘溫度、[工具] 查 SOP → 判斷原因：蒸汽壓不足，溫度會穩定' : '[工具] 讀 30 分鐘溫度、[工具] 查 SOP → 假設 H1：蒸汽壓不足，溫度會穩定', 'decision')
    push(at(K.plan), '指揮官', '拆步驟：S1 硫化公式、S2 訂單班表 → S3 排程（依賴 S1、S2）→ S4 確認（需核准）→ S5 通知；預期：30 分內 ≥ 121°C', 'info')
    push(at(K.sim), '工具', `[工具] S1：122°C 總需 28.5 h（+4.5 h）　[工具] S2：下一模 #A-311 交期 3 天後，夜班缺 1 人`, 'tool')
    push(at(K.gate), '求解器', '[工具] S3：#A-311 仍排 7 號機、夜班從 2 線調 1 人，交期全部滿足', 'tool')
    if (res1) {
      const txt = res1.d.decision === 'reject'
        ? `[人工] 主管駁回新排程${res1.d.reason ? `（${res1.d.reason}）` : ''}；維持 v1，寫入稽核日誌`
        : `動作把關確認可行 → [人工] 主管核准${res1.auto ? '（示範腳本）' : ''}；寫入稽核日誌`
      push(res1.at, '主管', txt, 'human')
    }
    if (single && res1 && res1.d.decision !== 'reject') push(closeT, '系統', 'S5 已通知硫化機組、胎體／胎面膠夜班；事件結案', 'info')
    if (!single) push(SAFETY_T, '安全核心', '7 號降至 120°C 以下 → 依規則保溫保壓（cure hold），不等 AI', 'safety')
    if (!single) push(VERIFY, '驗證（程式）', '比對預期：溫度降到 119.5°C → H1 不成立；安全核心已依規則處置', 'dynamic')
    if (!single) push(H2_T, '指揮官', '推翻 H1 → H2：持續洩壓或感測器漂移；[工具] 對照 6、8 號同管線 → [工具] SOP「硫化中斷處置」→ 7 號停用重排', 'decision')
    if (res2 && !single) {
      const opt = res2.d.option_id === 'B' ? 'B' : 'A'
      const txt = res2.d.decision === 'reject'
        ? `[人工] 主管兩案皆駁回${res2.d.reason ? `（${res2.d.reason}）` : ''}；7 號停用、維持現行排程`
        : opt === 'A' ? `[人工] 否決跨組調人，改選夜班加班；決定與理由寫入紀錄${res2.auto ? '（示範腳本）' : ''}`
          : '[人工] 核准從 2 線調 1 人；決定與理由寫入紀錄'
      push(res2.at, '主管', txt, 'human')
      push(d2 + 10 * SEC, '系統', 'S5 已通知硫化機組、胎體／胎面膠夜班；事件結案', 'info')
    }

    // ---- plan + steps ----
    const s1 = step('S1', 'cure_formula', { size: 'S', ref_temp_c: 128, actual_temp_c: 122, elapsed_h: 14.4, activation_energy_kj_mol: 85 }, [], '122°C 下等效硫化還需多久')
    const s2 = step('S2', 'query_orders_shifts', { press_ids: ['CURE-07'] }, [], '下一模訂單交期與夜班人力')
    const s3 = step('S3', 'solve_schedule', { cure_overrides_h: { 'CURE-07': '$S1.total_cure_h' }, allow_cross_team: true }, ['S1', 'S2'], '重排硫化機，交期不延誤')
    const s4 = step('S4', 'confirm_schedule', { schedule_version: 2, reason: '7 號延長硫化 4.5 h' }, ['S3'], '主管核准新排程')
    const s5 = step('S5', 'notify_teams', { teams: ['curing', 'body'], kind: 'info', message: '7 號延長硫化至 28.5 h，夜班從 2 線調 1 人' }, ['S4'], '通知硫化機組與夜班')
    const st = (t: number, after: StepRun['state'], before: StepRun['state'] = 'pending') => (now >= t ? after : before)
    let steps: StepRun[]
    let plan: Event['plan']
    if (!round2) {
      steps = [
        run(s1, st(at(K.sim), 'ok', now >= at(K.plan) ? 'running' : 'pending'), now >= at(K.sim) ? result('cure_formula', { rate_ratio: 0.679, remaining_ref_h: 9.6, remaining_actual_h: 14.1, total_cure_h: 28.5, extra_h: 4.5, formula: 'Arrhenius Ea=85 kJ/mol' }, 3) : null),
        run(s2, st(at(K.sim), 'ok', now >= at(K.plan) ? 'running' : 'pending'), now >= at(K.sim) ? result('query_orders_shifts', { next_order: '#A-311', due_in_days: 3, gaps: [`${NIGHT} 夜班缺 1 人`] }, 18) : null),
        run(s3, st(at(K.gate), 'ok', now >= at(K.sim) ? 'running' : 'pending'), now >= at(K.gate) ? result('solve_schedule', { feasible: true, solver: 'cp-sat', moves: [], staffing: ['夜班從 2 線調 1 人'], all_due_met: true, proposed_version: 2 }, 1840) : null),
        run(s4, res1 ? (res1.d.decision === 'reject' ? 'rejected' : 'ok') : st(R1_CREATED, 'awaiting_approval'), res1 && res1.d.decision !== 'reject' ? result('confirm_schedule', { active_version: 2 }, 12) : null, now >= at(K.gate) ? gate('S4', 'confirm_schedule', 'R3') : null),
        run(s5, res1 && now >= d1 + SEC ? (res1.d.decision === 'reject' ? 'skipped' : 'ok') : 'pending', res1 && res1.d.decision !== 'reject' && now >= d1 + SEC ? result('notify_teams', { sent: 2 }, 6) : null, now >= at(K.gate) ? gate('S5', 'notify_teams', 'R1', 'allow') : null),
      ]
      plan = now >= at(K.plan) ? {
        goal: '7 號機溫降，大胎與後續訂單不得延誤', hypothesis: H1, rejected_hypothesis_ids: [], steps: [s1, s2, s3, s4, s5],
        alert_card: single ? { title: 'CURE-07 溫度異常 128→122.3°C', summary: '蒸汽壓不足；延長硫化 4.5 h，後續訂單維持排程' } : { title: '7 號機溫降 128→122°C', summary: '假設 H1 蒸汽壓不足；延長硫化 4.5 h，#A-311 維持 7 號機' }, safe_stop: false,
        rationale: '溫度仍在 120–130°C，先延長硫化保品質，再以求解器重排避免交期延誤',
      } : null
    } else {
      const q1 = step('S1', 'read_sensors', { machine_ids: ['CURE-06', 'CURE-07', 'CURE-08'], minutes: 30 }, [], '對照 6、8 號同管線（H2 蒸汽頭）')
      const q2 = step('S2', 'sop_search', { query: '硫化中斷處置', k: 3 }, [], '查 SOP「硫化中斷處置」')
      const q3 = step('S3', 'solve_schedule', { unavailable_presses: ['CURE-07'], allow_cross_team: true, allow_overtime: true }, ['S1', 'S2'], '7 號停用後重排')
      const q4 = step('S4', 'confirm_schedule', { schedule_version: 3, reason: '7 號停用重排' }, ['S3'], '主管選擇 A 夜班加班 / B 從 2 線調 1 人')
      const q5 = step('S5', 'notify_teams', { teams: ['curing', 'body', 'maintenance'], kind: 'machine_out', machine_id: 'CURE-07', message: '7 號機停用' }, ['S4'], '通知各組與維修')
      steps = [
        run(q1, st(at(1030), 'ok', 'running'), now >= at(1030) ? result('read_sensors', { summary: { 'CURE-06': { temp_c_now: 127.8 }, 'CURE-07': { temp_c_now: 119.5 }, 'CURE-08': { temp_c_now: 128.1 } } }, 22) : null),
        run(q2, st(at(1030), 'ok', 'running'), now >= at(1030) ? result('sop_search', { hits: [{ sop_id: 'SOP-CI-03', title: '硫化中斷處置', score: 0.91 }] }, 35) : null),
        run(q3, st(at(1031), 'ok', now >= at(1030) ? 'running' : 'pending'), now >= at(1031) ? result('solve_schedule', { feasible: true, solver: 'cp-sat', proposed_version: 3, all_due_met: true, options: ['A 夜班加班', 'B 從 2 線調 1 人'] }, 2100) : null),
        run(q4, res2 ? (res2.d.decision === 'reject' ? 'rejected' : 'ok') : st(R2_CREATED, 'awaiting_approval'), res2 && res2.d.decision !== 'reject' ? result('confirm_schedule', { active_version: 3, option: res2.d.option_id }, 10) : null, now >= at(1031) ? gate('S4', 'confirm_schedule', 'R3') : null),
        run(q5, res2 && now >= d2 + 3 * SEC ? 'ok' : 'pending', res2 && now >= d2 + 3 * SEC ? result('notify_teams', { sent: 3 }, 6) : null, now >= at(1031) ? gate('S5', 'notify_teams', 'R3') : null),
      ]
      plan = now >= H2_T ? {
        goal: '7 號機溫降，大胎與後續訂單不得延誤', hypothesis: H2, rejected_hypothesis_ids: ['H1'], steps: [q1, q2, q3, q4, q5],
        alert_card: { title: '7 號機停用，重排中', summary: 'H1 不成立（119.5°C）；改 H2 洩壓／感測器漂移，依 SOP 停用 7 號' }, safe_stop: false,
        rationale: '預期 ≥121°C 未達成；同管線 6、8 號正常，排除總管蒸汽不足',
      } : null
    }
    const hyps: Hypothesis[] = round2 ? [{ ...H1, status: 'rejected' }, ...(now >= H2_T ? [{ ...H2, status: (state === 'closed' ? 'confirmed' : 'active') as Hypothesis['status'] }] : [])] : now >= at(K.hyp) ? [{ ...H1, status: (single && state === 'closed' ? 'confirmed' : 'active') as Hypothesis['status'] }] : []

    // ---- approvals ----
    const approvals: ApprovalRequest[] = []
    if (now >= R1_CREATED) approvals.push(approval({
      id: 'APR-P7-1', event: EV, step: 'S4', round: 1, created: R1_CREATED, risk: 'R3',
      title: '7 號延長硫化 4.5 h，夜班從 2 線調 1 人', reason: single ? '7 號 128 → 122.3°C；硫化公式：總需 28.5 h（+4.5 h）' : '7 號 128 → 122°C；S1 硫化公式：總需 28.5 h（+4.5 h）',
      done: ['安全核心監看中（仍在 120–130°C）', '已讀 30 分鐘溫度與 SOP'], gaps: [`${NIGHT} 夜班缺 1 名操作員`], impact: '交期全部滿足',
      options: [{ id: 'A', label: '核准新排程（夜班從 2 線調 1 人）', tool: 'confirm_schedule', args: { schedule_version: 2 }, recommended: true }],
      res: res1,
    }))
    if (!single && now >= R2_CREATED) approvals.push(approval({
      id: 'APR-P7-2', event: EV, step: 'S4', round: 2, created: R2_CREATED, risk: 'R3',
      title: '7 號機停用，重排中', reason: 'H1 不成立：溫度續降至 119.5°C',
      done: ['安全核心已處置', '#A-311 維持 7 號'], gaps: [`${NIGHT} 夜班缺 1 名操作員`], impact: '不受影響',
      options: [
        { id: 'A', label: '夜班加班', tool: 'confirm_schedule', args: { schedule_version: 3, staffing: 'overtime' }, recommended: false },
        { id: 'B', label: '從 2 線調 1 人', tool: 'confirm_schedule', args: { schedule_version: 3, staffing: 'transfer' }, recommended: true },
      ],
      res: res2,
    }))

    // ---- safety ----
    const safety: SafetyAction[] = !single && now >= SAFETY_T ? [{
      id: 'SA-CURE-07-1', ts: isoTaipei(SAFETY_T), machine_id: 'CURE-07', rule_id: 'CURE_LOW_HOLD', action: 'cure_hold',
      value: 119.9, threshold: 120, message: '7 號模溫 < 120°C：保溫保壓（cure hold），不等 AI', latency_ms: 3,
    }] : []

    // ---- focus (③) ----
    const tNow = r1(temp07(now))
    const outOfService = !single && now >= H2_T
    const elapsedH = r1(14.4 + (now - A) / HOUR)
    const auto: string[] = ['安全核心監看中：120–130°C 內不停機']
    if (now >= at(K.sim)) auto.push('S1 硫化公式：122°C 總需 28.5 h（+4.5 h）')
    if (now >= at(K.gate)) auto.push('S3 求解：#A-311 仍排 7 號機，交期全部滿足')
    if (res1 && res1.d.decision !== 'reject') auto.push('新排程 v2 已生效')
    if (!single && now >= SAFETY_T) auto.push('安全核心已保溫保壓（< 120°C）')
    if (outOfService) auto.push('7 號機停用（SOP 硫化中斷處置）')
    const pend = approvals.filter((a) => a.status === 'pending')
    const focus: FocusPanel = {
      event_id: EV, machine_id: 'CURE-07',
      title: outOfService ? '異常：7 號硫化機　停用重排' : single ? '異常：7 號硫化機（CURE-07）　溫度異常' : '異常：7 號硫化機　溫度下降',
      detail: `模溫 ${tNow}°C（硫化範圍 120–130°C；低於 120°C 安全核心直接保溫保壓）；` +
        (round2 ? 'H1 不成立 → H2：持續洩壓或感測器漂移' : single ? '原因：蒸汽壓偏低，溫度預測穩定在 122°C 附近' : '假設 H1：蒸汽壓不足，溫度會穩定'),
      tiles: [
        { label: '溫度', value: `${tNow}°C`, status: 'alarm' },
        { label: '壓力', value: '正常', status: 'normal' },
        { label: '時間', value: `${elapsedH} h`, status: 'normal' },
        { label: '蒸汽', value: '偏低', status: 'alarm' },
      ],
      auto_done: auto,
      pending: pend.flatMap((a) => a.options.map((o) => ({ approval_id: a.id, option_id: o.id, label: `${o.id} ${o.label}` }))),
      schedule_impact: round2 ? '7 號停用；#A-311 已改 4 號機，交期不受影響' : now >= at(K.gate) ? '#A-311 維持 7 號機；交期全部滿足' : '計算中…',
      state, timeline: T,
    }

    const event: Event = {
      id: EV, opened_at: isoTaipei(A), machine_id: 'CURE-07',
      anomaly: {
        id: `AN-${EV.slice(3)}`, ts: isoTaipei(A), machine_id: 'CURE-07', kind: 'temp_drop', channel: 'temp_c', value: single ? 122.3 : 122, baseline: 128,
        prediction: { machine_id: 'CURE-07', channel: 'temp_c', now_value: 122, predicted_value: 122, band_lo: 121, band_hi: 123, horizon_s: 1800, trend_per_min: -0.1, model: 'int8-ts' },
        severity: 'critical', message: single ? '7 號 128 → 122.3°C，預測穩定在 122±1°C' : '7 號 128 → 122°C，預測穩定在 122±1°C', related_machines: ['CURE-06', 'CURE-08'], detection_latency_s: 38,
      },
      state, goal: now >= at(K.plan) ? '7 號機溫降，大胎與後續訂單不得延誤' : null, hypotheses: hyps, plan, steps: plan ? steps : [],
      approvals: approvals.map((a) => a.id), safety_actions: safety, timeline: T, alert_card: plan?.alert_card ?? null,
      consecutive_failures: 0, replans: round2 ? 1 : 0, closed_at: state === 'closed' ? isoTaipei(closeT) : null,
      outcome: state === 'closed' && single ? '7 號延長硫化至 28.5 h；排程已更新' : state === 'closed' ? (res2?.d.decision === 'reject' ? '7 號停用；維持現行排程' : res2?.d.option_id === 'B' ? '7 號停用；從 2 線調 1 人' : '7 號停用；夜班加班') : null,
    }
    return {
      event, approvals, safety, focus: state === 'closed' ? { ...focus, pending: [] } : focus,
      machine: {
        'CURE-07': {
          status: outOfService ? 'out_of_service' : single && state === 'closed' ? 'running' : 'alarm', active_event_id: state === 'closed' ? null : EV,
          note: outOfService ? '停用：持續洩壓或感測器漂移（待維修確認）' : single && state === 'closed' ? '延長硫化中（排程已更新）' : !single && now >= SAFETY_T ? '保溫保壓中' : '溫度下降',
        },
      },
    }
  }

  return {
    id: 'press7', machine: 'CURE-07', versions, world,
    signal(id, t) {
      if (id === 'CURE-07') {
        const rel = (t - A) / SEC
        const steam = rel < -240 ? 6.5 : rel < (single ? 1e9 : 1020) ? lerp(6.5, 5.2, (rel + 240) / 240) : 4.8
        return { temp_c: temp07(t), steam_kgf_cm2: steam + noise('s7', t, 0.03) }
      }
      return null
    },
  }
}

// =====================================================================================
// 自動播放 Demo（?mock=1&demo=cure07）的訂單段：急單進來 → Agent 規劃 → 主管核准 → 完成（PLANT 事件，不是機台異常）
// =====================================================================================
export function rushOrderScript(A: number): Script {
  const EV = `EV-${isoTaipei(A).slice(0, 10).replace(/-/g, '')}-${isoTaipei(A).slice(11, 19).replace(/:/g, '')}-RUSH`
  const at = (s: number) => A + s * SEC
  const K = { plan: 4, sim: 10, card: 20 }
  const CARD = at(K.card)
  const res = (now: number, ctx: Ctx) => resolve(ctx, 'APR-RUSH-1', CARD, now, null,
    { decision: 'approve', option_id: 'A', reason: '', decided_by: '主管（示範腳本）', via: 'auto' })
  function world(now: number, ctx: Ctx): ScriptWorld {
    const empty: ScriptWorld = { event: null, approvals: [], safety: [], focus: null, machine: {} }
    if (now < A) return empty
    const r = res(now, ctx)
    const d = r?.at ?? Infinity
    const closeT = d + 30 * SEC
    const state = stateAt(now, [[A, 'detected'], [at(1), 'classify'], [at(K.plan), 'plan'], [at(K.sim), 'simulate'], [at(K.sim + 4), 'gate'],
      [CARD, 'await_approval'], [d, 'execute'], [d + 15 * SEC, 'verify'], [closeT, 'closed']])
    const T: TimelineItem[] = []
    const push = (t: number, a: TimelineItem['actor'], text: string, tag: TimelineItem['tag']) => { if (now >= t) T.push(tl(t, a, text, tag)) }
    push(A, '事件偵測', '急單 SO-017 到單：小胎 8 條，交期 10/05 → 喚醒 AI 規劃', 'info')
    push(at(K.plan), '指揮官', '[工具] 查訂單與班表、[工具] 求解排程 → 把 SO-017 插進 4 號機空檔', 'decision')
    push(at(K.sim), '求解器', '[工具] 排程 v2：SO-017 排 4 號機，其他訂單交期全部滿足', 'tool')
    if (r) push(r.at, '主管', '[人工] 主管核准急單排程；寫入稽核日誌', 'human')
    push(closeT, '系統', '已通知硫化機組；急單任務完成', 'info')
    const s1 = step('S1', 'query_orders_shifts', { order_ids: ['SO-017'] }, [], '急單的數量、交期與班表')
    const s2 = step('S2', 'solve_schedule', { insert_orders: ['SO-017'] }, ['S1'], '把急單排進去，其他訂單不延誤')
    const s3 = step('S3', 'confirm_schedule', { reason: '急單 SO-017 插單' }, ['S2'], '主管核准新排程')
    const s4 = step('S4', 'notify_teams', { teams: ['curing'], kind: 'info', message: '急單 SO-017 排 4 號機' }, ['S3'], '通知硫化機組')
    const st = (t: number, after: StepRun['state'], before: StepRun['state'] = 'pending') => (now >= t ? after : before)
    const steps: StepRun[] = [
      run(s1, st(at(K.sim), 'ok', now >= at(K.plan) ? 'running' : 'pending'), now >= at(K.sim) ? result('query_orders_shifts', { order: 'SO-017', qty: 8, due_in_days: 2 }, 15) : null),
      run(s2, st(at(K.sim + 4), 'ok', now >= at(K.sim) ? 'running' : 'pending'), now >= at(K.sim + 4) ? result('solve_schedule', { feasible: true, solver: 'cp-sat', all_due_met: true, proposed_version: 2 }, 1500) : null),
      run(s3, r ? (r.d.decision === 'reject' ? 'rejected' : 'ok') : st(CARD, 'awaiting_approval'), r && r.d.decision !== 'reject' ? result('confirm_schedule', { active_version: 2 }, 10) : null, now >= at(K.sim + 4) ? gate('S3', 'confirm_schedule', 'R2') : null),
      run(s4, r && now >= d + 15 * SEC ? (r.d.decision === 'reject' ? 'skipped' : 'ok') : 'pending', r && r.d.decision !== 'reject' && now >= d + 15 * SEC ? result('notify_teams', { sent: 1 }, 6) : null, now >= at(K.sim + 4) ? gate('S4', 'notify_teams', 'R1', 'allow') : null),
    ]
    const HR: Hypothesis = {
      id: 'H1', statement: '4 號機有空檔，插單不影響其他交期', cause_code: 'rush_order', status: state === 'closed' ? 'confirmed' : 'active',
      expectation: null, evidence: ['SO-017 小胎 8 條，交期 2 天後', '4 號機今天有空檔'],
    }
    const card = { title: '急單 SO-017：小胎 8 條插單', summary: '排進 4 號機空檔，其他訂單交期不受影響' }
    const plan: Event['plan'] = now >= at(K.plan) ? {
      goal: '急單 SO-017 準時交貨，其他訂單不延誤', hypothesis: HR, rejected_hypothesis_ids: [], steps: [s1, s2, s3, s4],
      alert_card: card, safe_stop: false, rationale: '4 號機有空檔，插單不影響其他交期',
    } : null
    const approvals: ApprovalRequest[] = now >= CARD ? [approval({
      id: 'APR-RUSH-1', event: EV, step: 'S3', round: 1, created: CARD, risk: 'R2',
      title: '急單 SO-017 排進 4 號機', reason: '急單到單：小胎 8 條，交期 2 天後',
      done: ['已查訂單與班表', '求解器：交期全部滿足'], gaps: [], impact: '其他訂單交期不受影響',
      options: [{ id: 'A', label: '核准新排程（SO-017 排 4 號機）', tool: 'confirm_schedule', args: { schedule_version: 2 }, recommended: true }],
      res: r,
    })] : []
    const focus: FocusPanel = {
      event_id: EV, machine_id: 'PLANT', title: '訂單：急單 SO-017 插單', detail: '小胎 8 條，交期 2 天後；Agent 規劃排程，主管核准後生效',
      tiles: [{ label: '時間', value: '交期 2 天', status: 'warn' }],
      auto_done: now >= at(K.sim + 4) ? ['S1 查訂單與班表', 'S2 求解：排 4 號機，交期全部滿足'] : [],
      pending: approvals.filter((a) => a.status === 'pending').flatMap((a) => a.options.map((o) => ({ approval_id: a.id, option_id: o.id, label: `${o.id} ${o.label}` }))),
      schedule_impact: now >= at(K.sim + 4) ? 'SO-017 排 4 號機；其他訂單交期全部滿足' : '計算中…', state, timeline: T,
    }
    const event: Event = {
      id: EV, opened_at: isoTaipei(A), machine_id: 'PLANT', source: 'plant',
      anomaly: {
        id: `AN-${EV.slice(3)}`, ts: isoTaipei(A), machine_id: 'PLANT', kind: 'order_rush', channel: 'temp_c', value: 0, baseline: 0,
        prediction: null, severity: 'warn', message: '急單 SO-017 到單：小胎 8 條', related_machines: [], detection_latency_s: null,
      },
      state, goal: plan?.goal ?? null, hypotheses: plan ? [HR] : [], plan, steps: plan ? steps : [], approvals: approvals.map((a) => a.id),
      safety_actions: [], timeline: T, alert_card: plan ? card : null, consecutive_failures: 0, replans: 0,
      closed_at: state === 'closed' ? isoTaipei(closeT) : null, outcome: state === 'closed' ? '急單 SO-017 排進 4 號機；交期全部滿足' : null,
    }
    return { event, approvals, safety: [], focus: state === 'closed' ? null : focus, machine: {} }
  }
  return { id: 'rush', machine: 'PLANT', world, signal: () => null }
}

// =====================================================================================
// Page 2 — A 線大開煉機 溫度過高 (cooling insufficient)
// =====================================================================================
/** tag: suffix for the approval / safety ids when the script is overlaid (情境疊加) beside an earlier copy. */
export function openMillScript(A: number, tag = ''): Script {
  const EV = `EV-${isoTaipei(A).slice(0, 10).replace(/-/g, '')}-${isoTaipei(A).slice(11, 19).replace(/:/g, '')}-A-BIGOPEN`
  const F = A - 60 * SEC                         // fault start; detected within 1 minute (page 1)
  const at = (s: number) => A + s * SEC
  const CREATED = at(13)
  const APR = `APR-OM-1${tag}`
  const rising = (t: number) => {
    const rel = (t - F) / SEC
    if (rel < 0) return 98
    if (rel < 840) return 98 + (14 * rel) / 840
    return 112 + (0.25 * (rel - 840)) / 60
  }
  const STOP_T = F + (840 + ((120 - 112) / 0.25) * 60) * SEC   // reaches 120 °C if nobody acts
  const dec = (now: number, ctx: Ctx) => resolve(ctx, APR, CREATED, now, null, { decision: 'approve' })
  const effect = (ctx: Ctx, now: number) => {
    const r = dec(now, ctx)
    return r && r.d.decision !== 'reject' && r.at < STOP_T ? r.at + 60 * SEC : null
  }
  const temp = (t: number, ctx: Ctx) => {
    const e = effect(ctx, t)
    if (e != null && t > e) { const v0 = rising(e); return 100 + (v0 - 100) * Math.exp(-(t - e) / (300 * SEC)) }
    if (t > STOP_T) return 120 - 25 * (1 - Math.exp(-(t - STOP_T) / (600 * SEC)))
    return rising(t)
  }
  const crossing108 = (ctx: Ctx, now: number) => {
    const e = effect(ctx, now)
    if (e == null) return Infinity
    const v0 = rising(e)
    return v0 <= 108 ? e : e + 300 * SEC * Math.log((v0 - 100) / 8)
  }

  function world(now: number, ctx: Ctx): ScriptWorld {
    if (now < A) return { event: null, approvals: [], safety: [], focus: null, machine: {} }
    const res = dec(now, ctx)
    const d = res?.at ?? Infinity
    const ok = !!res && res.d.decision !== 'reject'
    const cross = crossing108(ctx, Infinity)
    const stopped = now >= STOP_T && !(ok && d < STOP_T)
    const marks: [number, EventState][] = [
      [A, 'detected'], [at(1), 'classify'], [at(6), 'plan'], [at(8), 'simulate'], [at(12), 'gate'], [CREATED, 'await_approval'],
      [d, 'execute'], [d + 2 * SEC, 'verify'],
    ]
    if (ok) marks.push([cross, 'closed'])
    else if (res) marks.push([d + 3 * SEC, 'closed'])
    if (stopped && !res) marks.push([STOP_T, 'safe_stop'])
    const state = stateAt(now, marks)
    const T: TimelineItem[] = []
    const push = (t: number, a: TimelineItem['actor'], text: string, tag: TimelineItem['tag']) => { if (now >= t) T.push(tl(t, a, text, tag)) }
    const tA = r1(rising(A))
    push(A, '事件偵測', `[工具] 大開煉機膠溫 98 → ${tA}°C 持續上升、水流 40 → 22 L/min → 喚醒指揮官`, 'tool')
    push(at(1), '安全核心', `膠溫 ${tA}°C 未達未加硫上限 120°C（已加硫另設 110°C），不停機`, 'safety')
    push(at(4), '指揮官', '[工具] 讀 30 分鐘溫度與水流、[工具] 查 SOP → 假設 H1：冷卻水路流量不足，冷卻能力不足', 'decision')
    push(at(6), '指揮官', '拆步驟：S1 讀感測、S2 異常預測 → S3 查 SOP → S4 通知維修（自動）→ S5 放慢生料投入（需核准）；預期：15 分內 ≤ 108°C', 'info')
    push(at(8), '工具', '[工具] S2：預測 30 分後約 124°C（±2）— 會觸及未加硫上限 120°C', 'tool')
    push(at(12), '系統', '動作把關：通知維修 R1 自動執行；放慢生料投入 R3 送主管核准', 'info')
    if (res) push(d, '主管', ok ? '[人工] 核准放慢生料投入；寫入稽核日誌' : `[人工] 主管駁回放慢投料${res.d.reason ? `（${res.d.reason}）` : ''}；持續監控`, 'human')
    if (now >= STOP_T && !(ok && d < STOP_T)) push(STOP_T, '安全核心', '膠溫達 120°C（未加硫上限）→ 依規則停機，不等 AI', 'safety')
    if (ok && cross < Infinity) push(cross, '驗證（程式）', '比對預期：膠溫回落至 108°C → H1 成立，事件結案', 'dynamic')

    const s1 = step('S1', 'read_sensors', { machine_ids: ['A-BIGOPEN'], minutes: 30 }, [], '讀膠溫與冷卻水流')
    const s2 = step('S2', 'predict_anomaly', { machine_id: 'A-BIGOPEN', channel: 'temp_c', horizon_min: 30 }, [], '預測是否觸及 120°C')
    const s3 = step('S3', 'sop_search', { query: '開煉機 膠溫過高 冷卻', k: 3 }, ['S1', 'S2'], '查開煉機過熱 SOP')
    const s4 = step('S4', 'notify_teams', { teams: ['maintenance'], kind: 'maintenance', machine_id: 'A-BIGOPEN', message: '冷卻水路流量偏低' }, ['S3'], '通知維修冷卻水路')
    const s5 = step('S5', 'notify_teams', { teams: ['bigopen'], kind: 'slow_feed', machine_id: 'A-BIGOPEN', message: '放慢生料投入' }, ['S3'], '放慢生料投入（人執行）')
    const hasPlan = now >= at(6)
    const steps: StepRun[] = hasPlan ? [
      run(s1, now >= at(8) ? 'ok' : 'running', now >= at(8) ? result('read_sensors', { summary: { 'A-BIGOPEN': { temp_c_now: tA, water_flow_lpm: 22 } } }, 20) : null),
      run(s2, now >= at(8) ? 'ok' : 'running', now >= at(8) ? result('predict_anomaly', { predicted_value: 124, band_lo: 122, band_hi: 126, horizon_s: 1800 }, 9) : null),
      run(s3, now >= at(12) ? 'ok' : now >= at(8) ? 'running' : 'pending', now >= at(12) ? result('sop_search', { hits: [{ sop_id: 'SOP-OM-01', title: '開煉機膠溫異常處置', score: 0.88 }] }, 31) : null),
      run(s4, now >= at(12) ? 'ok' : 'pending', now >= at(12) ? result('notify_teams', { sent: 1 }, 5) : null, now >= at(12) ? gate('S4', 'notify_teams', 'R1', 'allow') : null),
      run(s5, res ? (ok ? 'ok' : 'rejected') : now >= CREATED ? 'awaiting_approval' : 'pending', ok ? result('notify_teams', { sent: 1 }, 5) : null, now >= at(12) ? gate('S5', 'notify_teams', 'R3') : null),
    ] : []
    const H1: Hypothesis = {
      id: 'H1', statement: '冷卻水路流量不足，冷卻能力不足', cause_code: 'cooling_insufficient',
      status: state === 'closed' && ok ? 'confirmed' : 'active',
      expectation: { machine_id: 'A-BIGOPEN', channel: 'temp_c', op: '<=', value: 108, value_hi: null, within_min: 15 }, evidence: ['水流 40 → 22 L/min', 'SOP-OM-01'],
    }
    const approvals: ApprovalRequest[] = now >= CREATED ? [approval({
      id: APR, event: EV, step: 'S5', round: 1, created: CREATED, risk: 'R3', title: '放慢生料投入（A 線大開煉機）',
      reason: `膠溫 ${tA}°C 持續上升，預測 30 分後約 124°C`, done: ['已自動通知維修冷卻水路'], gaps: [],
      impact: '無，大胎 #07 仍 17:00 入硫化機',
      options: [{ id: 'A', label: '放慢生料投入', tool: 'notify_teams', args: { kind: 'slow_feed', teams: ['bigopen'] }, recommended: true }], res,
    })] : []
    const tNow = r1(temp(now, ctx))
    const mins = Math.max(0, Math.floor((now - F) / MIN))
    const flowLow = !(ok && now > d + 300 * SEC)
    const focus: FocusPanel = {
      event_id: EV, machine_id: 'A-BIGOPEN', title: '異常：A 線大開煉機　溫度過高',
      detail: `膠溫 ${tNow}°C 且持續${state === 'closed' || (ok && now > d + 60 * SEC) ? '回落' : '上升'}（未加硫上限 120°C；已加硫另設 110°C 防焦燒）；原因：冷卻能力不足`,
      tiles: [
        { label: '溫度', value: `${Math.round(tNow)}°C`, status: tNow >= 105 ? 'alarm' : 'normal' },
        { label: '壓力', value: '正常', status: 'normal' },
        { label: '時間', value: `${mins} 分`, status: 'normal' },
        { label: '水流', value: flowLow ? '偏低' : '正常', status: flowLow ? 'alarm' : 'normal' },
      ],
      auto_done: now >= at(12) ? ['通知維修冷卻水路'] : [],
      pending: approvals.filter((a) => a.status === 'pending').map((a) => ({ approval_id: a.id, option_id: 'A', label: '放慢生料投入' })),
      approved: approvals.filter((a) => a.status === 'approved' || a.status === 'modified').map((a) => ({ approval_id: a.id, option_id: 'A', label: '放慢生料投入', decided_by: a.decision?.decided_by ?? '主管', status: a.status })),
      schedule_impact: '無，大胎 #07 仍 17:00 入硫化機', state, timeline: T,
    }
    const safety: SafetyAction[] = now >= STOP_T && !(ok && d < STOP_T) ? [{
      id: `SA-A-BIGOPEN-1${tag}`, ts: isoTaipei(STOP_T), machine_id: 'A-BIGOPEN', rule_id: 'COMPOUND_UNVULC_120', action: 'stop_machine',
      value: 120, threshold: 120, message: '未加硫膠 ≥120°C：停機', latency_ms: 2,
    }] : []
    const event: Event = {
      id: EV, opened_at: isoTaipei(A), machine_id: 'A-BIGOPEN',
      anomaly: {
        id: `AN-${EV.slice(3)}`, ts: isoTaipei(A), machine_id: 'A-BIGOPEN', kind: 'overheat', channel: 'temp_c', value: tA, baseline: 98,
        prediction: { machine_id: 'A-BIGOPEN', channel: 'temp_c', now_value: tA, predicted_value: 124, band_lo: 122, band_hi: 126, horizon_s: 1800, trend_per_min: 0.875, model: 'int8-ts' },
        severity: 'critical', message: '大開煉機膠溫持續上升、冷卻水流偏低', related_machines: [], detection_latency_s: 60,
      },
      state, goal: hasPlan ? '大開煉機膠溫回穩，不整批報廢，大胎 #07 準時入硫化機' : null, hypotheses: now >= at(4) ? [H1] : [],
      plan: hasPlan ? {
        goal: '大開煉機膠溫回穩，不整批報廢，大胎 #07 準時入硫化機', hypothesis: H1, rejected_hypothesis_ids: [], steps: [s1, s2, s3, s4, s5],
        alert_card: { title: 'A 線大開煉機溫度過高', summary: '冷卻能力不足；已通知維修，待核准放慢生料投入' }, safe_stop: false,
        rationale: '預熱過熱會提早硬化，整批報廢風險；先降投料熱負荷，維修處理水路',
      } : null,
      steps, approvals: approvals.map((a) => a.id), safety_actions: safety, timeline: T,
      alert_card: hasPlan ? { title: 'A 線大開煉機溫度過高', summary: '冷卻能力不足；已通知維修，待核准放慢生料投入' } : null,
      consecutive_failures: 0, replans: 0, closed_at: state === 'closed' ? isoTaipei(ok ? cross : d + 3 * SEC) : null,
      outcome: state === 'closed' ? (ok ? '膠溫回落，H1 成立' : '主管駁回；持續監控') : state === 'safe_stop' ? '安全核心停機' : null,
    }
    const closed = state === 'closed' || state === 'safe_stop'
    return {
      event, approvals, safety, focus: closed ? { ...focus, pending: [] } : focus,
      machine: { 'A-BIGOPEN': { status: stopped ? 'stopped' : closed && ok ? 'running' : 'alarm', active_event_id: closed ? null : EV, note: stopped ? '安全核心停機（≥120°C）' : null } },
    }
  }
  return {
    id: 'open_mill', machine: 'A-BIGOPEN', world,
    signal(id, t, ctx) {
      if (id !== 'A-BIGOPEN') return null
      const rel = (t - F) / SEC
      const r = dec(t, ctx)
      const repaired = r && r.d.decision !== 'reject' && t > r.at + 300 * SEC
      const flow = rel < 0 ? 40 : repaired ? 38 : lerp(40, 22, rel / 120)
      return { temp_c: temp(t, ctx) + noise(id, t, 0.12), water_flow_lpm: flow + noise('f', t, 0.3) }
    },
  }
}

// =====================================================================================
// concurrent_3 — a third, small event that closes itself on an R1 notification
// =====================================================================================
export function bmixScript(A: number): Script {
  const EV = `EV-${isoTaipei(A).slice(0, 10).replace(/-/g, '')}-${isoTaipei(A).slice(11, 19).replace(/:/g, '')}-B-MIX`
  const at = (s: number) => A + s * SEC
  return {
    id: 'bmix', machine: 'B-MIX',
    signal(id, t) {
      if (id !== 'B-MIX') return null
      const rel = (t - A) / SEC
      const bump = rel < -60 ? 0 : rel < 30 ? lerp(0, 7, (rel + 60) / 90) : lerp(7, 0, (rel - 30) / 240)
      return { temp_c: 106 + bump + noise(id, t) }
    },
    world(now): ScriptWorld {
      if (now < A) return { event: null, approvals: [], safety: [], focus: null, machine: {} }
      const state = stateAt(now, [[A, 'detected'], [at(1), 'classify'], [at(3), 'plan'], [at(4), 'simulate'], [at(5), 'gate'], [at(6), 'execute'], [at(8), 'verify'], [at(270), 'closed']])
      const T: TimelineItem[] = [tl(A, '事件偵測', '[工具] B 線密煉機 106 → 113°C 上升 → 喚醒指揮官', 'tool')]
      if (now >= at(6)) T.push(tl(at(6), '系統', '動作把關：通知胎側膠組檢查投料 R1 自動執行', 'info'))
      if (now >= at(270)) T.push(tl(at(270), '驗證（程式）', '比對預期：回到 108°C 以下，事件結案', 'dynamic'))
      const s1 = step('S1', 'notify_teams', { teams: ['side'], kind: 'info', machine_id: 'B-MIX', message: '檢查投料' }, [], '通知胎側膠組')
      const event: Event = {
        id: EV, opened_at: isoTaipei(A), machine_id: 'B-MIX',
        anomaly: { id: `AN-${EV.slice(3)}`, ts: isoTaipei(A), machine_id: 'B-MIX', kind: 'overheat', channel: 'temp_c', value: 113, baseline: 106, prediction: null, severity: 'warn', message: 'B 線密煉機溫度偏高', related_machines: [], detection_latency_s: 30 },
        state, goal: '密煉溫度回穩', hypotheses: [], plan: null,
        steps: [run(s1, now >= at(6) ? 'ok' : 'pending', null, gate('S1', 'notify_teams', 'R1', 'allow'))],
        approvals: [], safety_actions: [], timeline: T, alert_card: { title: 'B 線密煉機溫度偏高', summary: '已自動通知胎側膠組' },
        consecutive_failures: 0, replans: 0, closed_at: state === 'closed' ? isoTaipei(at(270)) : null, outcome: state === 'closed' ? '自行回穩' : null,
      }
      return { event, approvals: [], safety: [], focus: null, machine: { 'B-MIX': { status: state === 'closed' ? 'running' : 'warning', active_event_id: state === 'closed' ? null : EV } } }
    },
  }
}

// =====================================================================================
// 情境疊加 (scenario overlay) — one fault on any machine of a kind
// =====================================================================================
export type OverlayFault = 'steam' | 'water' | 'heat'

/** A fault for an overlay step that cannot reuse its story script: its machine is busy and an idle one of the same
 *  kind stands in, or the script cannot be stacked (press 7's also drives the schedule versions). The fault starts
 *  30 s before A and is detected at A. steam (a press) and water (a cooled mill) raise an R3 card that waits for the
 *  supervisor and close 2 minutes after the approval; heat (a mixer) closes itself on an R1 notification, like bmix.
 *  base: the machine's normal temperature; tag: keeps ids apart from an earlier copy on the same machine. */
export function overlayFaultScript(A: number, mid: string, kind: OverlayFault, base: number, tag = ''): Script {
  const EV = `EV-${isoTaipei(A).slice(0, 10).replace(/-/g, '')}-${isoTaipei(A).slice(11, 19).replace(/:/g, '')}-${mid}`
  const F = A - 30 * SEC
  const at = (s: number) => A + s * SEC
  const CREATED = at(13)
  const APR = `APR-OV-${mid}${tag}`
  const label = machineLabel(mid)
  const card = kind !== 'heat'
  const drop = base >= 128 ? 6 : 4                // a small-tire press runs at 125.5 °C: stay above the 120 °C hold
  const low = r1(base - drop)
  const T: Record<OverlayFault, { title: string; detect: string; cause: string; ask: string; goal: string; kind: Event['anomaly']['kind'] }> = {
    steam: { title: `${label}溫度下降`, detect: `${label} ${base} → ${low}°C、蒸汽壓偏低`, cause: '蒸汽壓不足',
      ask: `${label}延長硫化，通知硫化機組`, goal: `${label}溫降，訂單不得延誤`, kind: 'temp_drop' },
    water: { title: `${label}溫度過高`, detect: `${label}冷卻水流 40 → 18 L/min、膠溫上升`, cause: '冷卻能力不足',
      ask: `放慢生料投入（${label}）`, goal: `${label}膠溫回穩，不整批報廢`, kind: 'overheat' },
    heat: { title: `${label}溫度偏高`, detect: `${label} ${base} → ${r1(base + 7)}°C 上升`, cause: '投料偏熱',
      ask: '', goal: '密煉溫度回穩', kind: 'overheat' },
  }
  const txt = T[kind]
  const dec = (now: number, ctx: Ctx) => (card ? resolve(ctx, APR, CREATED, now, null, { decision: 'approve' }) : null)
  /** 1 while the fault holds, falling to 0 over ~5 min once the approved action takes effect (a minute after it). */
  const hold = (t: number, ctx: Ctx) => {
    const r = dec(t, ctx)
    const e = r && r.d.decision !== 'reject' ? r.at + 60 * SEC : null
    return e != null && t > e ? Math.exp(-(t - e) / (300 * SEC)) : 1
  }
  return {
    id: `overlay-${mid}${tag}`, machine: mid,
    signal(id, t, ctx) {
      if (id !== mid || t < F) return null
      const f = Math.min(1, (t - F) / (30 * SEC)) * hold(t, ctx)
      if (kind === 'steam') return { temp_c: base - drop * f + noise(id, t, 0.05), steam_kgf_cm2: 6.5 - 1.3 * f + noise('s' + id, t, 0.03) }
      if (kind === 'water') return { temp_c: base + 12 * Math.min(1, (t - F) / (600 * SEC)) * hold(t, ctx) + noise(id, t, 0.12), water_flow_lpm: 40 - 22 * f + noise('f' + id, t, 0.3) }
      const rel = (t - A) / SEC
      return { temp_c: base + (rel < 30 ? lerp(0, 7, (rel + 30) / 60) : lerp(7, 0, (rel - 30) / 240)) + noise(id, t) }
    },
    world(now, ctx): ScriptWorld {
      if (now < A) return { event: null, approvals: [], safety: [], focus: null, machine: {} }
      const res = dec(now, ctx)
      const d = res?.at ?? Infinity
      const ok = !!res && res.d.decision !== 'reject'
      const closedAt = card ? (ok ? d + 120 * SEC : d + 3 * SEC) : at(270)
      const marks: [number, EventState][] = card
        ? [[A, 'detected'], [at(1), 'classify'], [at(6), 'plan'], [at(8), 'simulate'], [at(12), 'gate'], [CREATED, 'await_approval'],
            [d, 'execute'], [d + 2 * SEC, 'verify'], [closedAt, 'closed']]
        : [[A, 'detected'], [at(1), 'classify'], [at(3), 'plan'], [at(4), 'simulate'], [at(5), 'gate'], [at(6), 'execute'], [at(8), 'verify'], [closedAt, 'closed']]
      const state = stateAt(now, marks)
      const closed = state === 'closed'
      const TL: TimelineItem[] = []
      const push = (t: number, a: TimelineItem['actor'], text: string, tg: TimelineItem['tag']) => { if (now >= t) TL.push(tl(t, a, text, tg)) }
      push(A, '事件偵測', `[工具] ${txt.detect} → 喚醒指揮官`, 'tool')
      push(at(1), '安全核心', '仍在安全範圍內，不停機', 'safety')
      if (card) {
        push(at(4), '指揮官', `[工具] 讀 30 分鐘讀數、[工具] 查 SOP → 假設 H1：${txt.cause}`, 'decision')
        push(at(12), '系統', `動作把關：通知維修 R1 自動執行；${txt.ask} R3 送主管核准`, 'info')
        if (res) push(d, '主管', ok ? `[人工] 核准${txt.ask}；寫入稽核日誌` : '[人工] 主管駁回；持續監控', 'human')
        if (ok) push(closedAt, '驗證（程式）', '比對預期：讀數回穩 → H1 成立，事件結案', 'dynamic')
      } else {
        push(at(6), '系統', '動作把關：通知胎側膠組檢查投料 R1 自動執行', 'info')
        push(closedAt, '驗證（程式）', '比對預期：溫度回穩，事件結案', 'dynamic')
      }
      const s1 = step('S1', 'notify_teams', { teams: ['maintenance'], kind: 'maintenance', machine_id: mid, message: txt.cause }, [], '通知維修')
      const s2 = step('S2', 'notify_teams', { teams: kind === 'steam' ? ['curing'] : ['body'], kind: kind === 'steam' ? 'cure_extend' : 'slow_feed', machine_id: mid, message: txt.ask }, ['S1'], txt.ask)
      const steps: StepRun[] = card ? [
        run(s1, now >= at(12) ? 'ok' : 'pending', now >= at(12) ? result('notify_teams', { sent: 1 }, 5) : null, now >= at(12) ? gate('S1', 'notify_teams', 'R1', 'allow') : null),
        run(s2, res ? (ok ? 'ok' : 'rejected') : now >= CREATED ? 'awaiting_approval' : 'pending', ok ? result('notify_teams', { sent: 1 }, 5) : null, now >= at(12) ? gate('S2', 'notify_teams', 'R3') : null),
      ] : [run(s1, now >= at(6) ? 'ok' : 'pending', null, gate('S1', 'notify_teams', 'R1', 'allow'))]
      const H1: Hypothesis = {
        id: 'H1', statement: txt.cause, cause_code: kind === 'steam' ? 'steam_pressure_low' : 'cooling_insufficient',
        status: closed && ok ? 'confirmed' : 'active', expectation: null, evidence: [txt.detect],
      }
      const approvals: ApprovalRequest[] = card && now >= CREATED ? [approval({
        id: APR, event: EV, step: 'S2', round: 1, created: CREATED, risk: 'R3', title: txt.ask, reason: txt.detect,
        done: ['已自動通知維修'], gaps: [], impact: '交期不受影響',
        options: [{ id: 'A', label: txt.ask, tool: 'notify_teams', args: { kind: kind === 'steam' ? 'cure_extend' : 'slow_feed' }, recommended: true }], res,
      })] : []
      const alert = { title: txt.title, summary: card ? `${txt.cause}；已通知維修，待核准${txt.ask}` : '已自動通知胎側膠組' }
      const event: Event = {
        id: EV, opened_at: isoTaipei(A), machine_id: mid,
        anomaly: {
          id: `AN-${EV.slice(3)}`, ts: isoTaipei(A), machine_id: mid, kind: txt.kind, channel: 'temp_c', value: kind === 'steam' ? low : r1(base + 7), baseline: base,
          prediction: null, severity: card ? 'critical' : 'warn', message: txt.detect, related_machines: [], detection_latency_s: 30,
        },
        state, goal: txt.goal, hypotheses: card && now >= at(4) ? [H1] : [],
        plan: card && now >= at(6) ? { goal: txt.goal, hypothesis: H1, rejected_hypothesis_ids: [], steps: [s1, s2], alert_card: alert, safe_stop: false, rationale: `${txt.cause}；先通知維修，${txt.ask}由主管決定` } : null,
        steps, approvals: approvals.map((a) => a.id), safety_actions: [], timeline: TL, alert_card: alert,
        consecutive_failures: 0, replans: 0, closed_at: closed ? isoTaipei(closedAt) : null,
        outcome: closed ? (card ? (ok ? 'H1 成立，讀數回穩' : '主管駁回；持續監控') : '自行回穩') : null,
      }
      return { event, approvals, safety: [], focus: null, machine: { [mid]: { status: closed ? 'running' : card ? 'alarm' : 'warning', active_event_id: closed ? null : EV } } }
    },
  }
}
