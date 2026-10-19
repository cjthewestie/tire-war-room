// Visual approval card (issue #8): everything the card draws, derived from data the server already
// sends: the approval request, the event's steps and results, the schedule versions and the audit log.
// Nothing is hard-coded; a missing field leaves its part of the card out.
import type {
  ApprovalOption, ApprovalRequest, Event, RiskLevel, ScheduleJob, ScheduleVersion, StaffingChange, StepRun, ToolName,
} from '../contracts'
import { TEAM_LABELS } from '../factory'
import { ms } from '../format'
import { isRevoke, resultText } from '../stepText'
import type { ToolItem } from './transcript'

const WRITE_TOOLS: ToolName[] = ['confirm_schedule', 'set_cure_time', 'notify_teams']
type D = Record<string, unknown>
const arr = (v: unknown): D[] => (Array.isArray(v) ? (v as unknown[]).filter((x): x is D => !!x && typeof x === 'object') : [])
const num = (v: unknown): number | null => {
  const n = typeof v === 'number' ? v : typeof v === 'string' && v.trim() !== '' ? Number(v) : NaN
  return isFinite(n) ? n : null
}

// ---------------------------------------------------------------- plan flow
export type NodeStatus = 'done' | 'auto' | 'wait' | 'after' | 'pending' | 'running' | 'failed' | 'skipped' | 'rejected'
export interface PlanNode { id: string; tool: ToolName; purpose: string; deps: string[]; status: NodeStatus; risk: RiskLevel | null; level: number }

/** The current round's plan S1…Sn (approval-option and withdrawal steps are results of a decision, not plan steps). */
export function planNodes(ev: Event | null): PlanNode[] {
  if (!ev) return []
  const steps = ev.steps.filter((s) => !s.step.id.startsWith('AP-') && !isRevoke(s.step.id))
  const ids = new Set(steps.map((s) => s.step.id))
  const level = new Map<string, number>()
  const lv = (id: string, seen: Set<string> = new Set()): number => {
    if (level.has(id)) return level.get(id)!
    if (seen.has(id)) return 0
    seen.add(id)
    const sr = steps.find((s) => s.step.id === id)
    const deps = (sr?.step.depends_on ?? []).filter((d) => ids.has(d))
    const l = deps.length ? Math.max(...deps.map((d) => lv(d, seen))) + 1 : 0
    level.set(id, l)
    return l
  }
  return steps.map((sr) => ({
    id: sr.step.id, tool: sr.step.tool, purpose: sr.step.purpose, deps: sr.step.depends_on.filter((d) => ids.has(d)),
    status: nodeStatus(sr), risk: sr.gate?.risk ?? null, level: lv(sr.step.id),
  }))
}

function nodeStatus(sr: StepRun): NodeStatus {
  switch (sr.state) {
    case 'ok': return WRITE_TOOLS.includes(sr.step.tool) && sr.gate?.risk === 'R1' ? 'auto' : 'done'
    case 'awaiting_approval': return 'wait'
    case 'pending': return sr.gate?.verdict === 'allow' ? 'after' : 'pending'
    default: return sr.state
  }
}

export const NODE_LABEL: Record<NodeStatus, string> = {
  done: '完成', auto: '已自動', wait: '等主管核准', after: '核准後自動', pending: '未開始', running: '執行中', failed: '失敗', skipped: '未執行', rejected: '被攔截',
}

// ---------------------------------------------------------------- schedule before / after
export interface JobChange { job: ScheduleJob; before: ScheduleJob | null; kind: 'moved' | 'cure' | 'retimed' | 'new' }
export interface ScheduleDiff {
  before: ScheduleVersion; after: ScheduleVersion; presses: string[]; changes: JobChange[]
  moved: { order: string; from: string; to: string; n: number }[]
  cure: { press: string; from: number; to: number } | null
  t0: number; t1: number
}

/** The schedule version this round proposes: the solver's result, else the confirm step's args. */
export function proposedVersion(ev: Event | null, req: ApprovalRequest): number | null {
  const steps = ev?.steps ?? []
  for (const sr of [...steps].reverse()) {
    if (sr.step.tool === 'solve_schedule' && sr.result?.ok) {
      const v = num(sr.result.data.schedule_version) ?? num(sr.result.data.proposed_version)
      if (v != null) return v
    }
  }
  const confirm = steps.find((s) => s.step.tool === 'confirm_schedule' && (s.step.id === req.step_id || s.state === 'awaiting_approval'))
  const v = num(confirm?.step.args.schedule_version)
  if (v != null) return v
  for (const o of req.options) { const x = num(o.args.schedule_version); if (x != null) return x }
  return null
}

export function scheduleDiff(versions: ScheduleVersion[], proposed: number | null, cure: { press: string; from: number; to: number } | null, now: number): ScheduleDiff | null {
  const after = versions.find((v) => v.version === proposed)
  if (!after) return null
  const before = versions.find((v) => v.version === after.parent_version) ?? versions.find((v) => v.status === 'active' && v.version !== after.version)
  if (!before) return null
  const oldById = new Map(before.schedule.jobs.map((j) => [j.id, j]))
  // a job the solver re-created gets a new id: pair the rest per order by mold-in order
  const byOrder = new Map<string, ScheduleJob[]>()
  for (const j of [...before.schedule.jobs].sort((a, b) => ms(a.mold_in) - ms(b.mold_in))) byOrder.set(j.order_id, [...(byOrder.get(j.order_id) ?? []), j])
  const newIds = new Set(after.schedule.jobs.map((j) => j.id))
  const changes: JobChange[] = []
  const used = new Set<string>()
  for (const j of [...after.schedule.jobs].sort((a, b) => ms(a.mold_in) - ms(b.mold_in))) {
    let old = oldById.get(j.id) ?? null
    if (!old) old = (byOrder.get(j.order_id) ?? []).find((o) => !newIds.has(o.id) && !used.has(o.id)) ?? null
    if (old) used.add(old.id)
    if (!old) { changes.push({ job: j, before: null, kind: 'new' }); continue }
    if (old.press_id !== j.press_id) changes.push({ job: j, before: old, kind: 'moved' })
    else if (Math.abs(old.cure_h - j.cure_h) > 0.05) changes.push({ job: j, before: old, kind: 'cure' })
    else if (old.mold_in !== j.mold_in) changes.push({ job: j, before: old, kind: 'retimed' })
  }
  const key = changes.filter((c) => c.kind === 'moved' || c.kind === 'cure')
  const presses = new Set<string>()
  for (const c of key) { presses.add(c.job.press_id); if (c.before) presses.add(c.before.press_id) }
  if (cure) presses.add(cure.press)
  if (!presses.size) return null
  const grouped = new Map<string, { order: string; from: string; to: string; n: number }>()
  for (const c of changes) {
    if (c.kind !== 'moved' || !c.before) continue
    const k = `${c.job.order_id}|${c.before.press_id}|${c.job.press_id}`
    const g = grouped.get(k) ?? { order: c.job.order_id, from: c.before.press_id, to: c.job.press_id, n: 0 }
    g.n++
    grouped.set(k, g)
  }
  const cureJob = key.find((c) => c.kind === 'cure' && (!cure || c.job.press_id === cure.press))
  const cureOut = cure ?? (cureJob?.before ? { press: cureJob.job.press_id, from: cureJob.before.cure_h, to: cureJob.job.cure_h } : null)
  // window: the changed jobs (old and new bars), from at most a day before now
  const times = key.flatMap((c) => [c.job, c.before].filter((x): x is ScheduleJob => !!x)).flatMap((j) => [ms(j.mold_in), ms(j.demold)]).filter(isFinite)
  const H = 3_600_000
  let t0 = Math.min(...times, isFinite(now) ? now : Infinity) - 2 * H
  if (isFinite(now)) t0 = Math.max(t0, now - 24 * H)
  let t1 = Math.max(...times, isFinite(now) ? now + 12 * H : -Infinity) + 2 * H
  t1 = Math.min(t1, t0 + 7 * 24 * H)
  return { before, after, presses: [...presses].sort(), changes, moved: [...grouped.values()], cure: cureOut, t0, t1 }
}

/** Cure-time change of the event's press from the cure_formula step: reference hours → formula total. */
export function cureChange(ev: Event | null): { press: string; from: number; to: number } | null {
  if (!ev) return null
  const sr = [...ev.steps].reverse().find((s) => s.step.tool === 'cure_formula' && s.result?.ok)
  if (!sr?.result) return null
  const to = num(sr.result.data.total_cure_h)
  const from = num(sr.step.args.ref_cure_h) ?? num(sr.result.data.ref_cure_h)
  return to != null && from != null ? { press: ev.machine_id, from, to } : null
}

// ---------------------------------------------------------------- staffing options
export interface OptionView {
  opt: ApprovalOption; who: string | null; shift: string | null; people: number | null; kind: string | null; teams: string[]; message: string | null
}

export function optionViews(req: ApprovalRequest, ev: Event | null): OptionView[] {
  const solve = ev ? [...ev.steps].reverse().find((s) => s.step.tool === 'solve_schedule' && s.result?.ok) : undefined
  const staffing = arr(solve?.result?.data.staffing_options) as unknown as StaffingChange[]
  return req.options.map((opt) => {
    const sc = staffing.find((s) => s.label === opt.label) ?? staffing.find((s) => opt.label.includes(s.label) || s.label.includes(opt.label))
    const teams = Array.isArray(opt.args.teams) ? (opt.args.teams as string[]).map((t) => TEAM_LABELS[t as keyof typeof TEAM_LABELS] ?? t) : []
    const to = sc ? TEAM_LABELS[sc.to_team] ?? sc.to_team : null
    const who = sc ? (sc.kind === 'transfer' ? `${sc.from_label ?? (sc.from_team ? TEAM_LABELS[sc.from_team] : '其他組')} → ${to}` : `${to}（原班人員加班）`) : null
    const shift = sc ? `${sc.shift_date.slice(5).replace('-', '/')} ${sc.shift_name}` : null
    return { opt, who, shift, people: sc?.people ?? null, kind: sc?.kind ?? (typeof opt.args.kind === 'string' ? opt.args.kind : null), teams, message: typeof opt.args.message === 'string' ? opt.args.message : null }
  })
}

/** What the solver says about deliveries for this round. */
export function deliveryFacts(ev: Event | null): { met: boolean | null; late: string[] } {
  const solve = ev ? [...ev.steps].reverse().find((s) => s.step.tool === 'solve_schedule' && s.result?.ok) : undefined
  const d = solve?.result?.data
  if (!d) return { met: null, late: [] }
  return { met: typeof d.deliveries_met === 'boolean' ? d.deliveries_met : null, late: Array.isArray(d.late_orders) ? d.late_orders.map(String) : [] }
}

// ---------------------------------------------------------------- tools used
export interface ToolUse { tool: ToolName; calls: number; failed: number; line: string }

/** Per tool: how many calls this event made (audit when present, else the steps) and the latest result in words. */
export function toolUses(ev: Event | null, tools: ToolItem[]): ToolUse[] {
  const out = new Map<ToolName, ToolUse>()
  const add = (tool: ToolName, ok: boolean, args: D, data: D | null) => {
    const u = out.get(tool) ?? { tool, calls: 0, failed: 0, line: '' }
    u.calls++
    if (!ok) u.failed++
    else if (data) { try { u.line = resultText(tool, args, data) } catch { /* keep the previous line */ } }
    out.set(tool, u)
  }
  if (tools.length) {
    for (const t of tools) if (t.status !== 'running') add(t.tool, t.status === 'ok', t.args, t.data)
  } else if (ev) {
    for (const sr of [...(ev.prior_rounds ?? []).flatMap((r) => r.steps), ...ev.steps]) {
      if (sr.result) add(sr.step.tool, sr.result.ok, sr.step.args, sr.result.data)
    }
  }
  return [...out.values()]
}
