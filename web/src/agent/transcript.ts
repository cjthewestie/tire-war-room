// Agent console (issue #8): turns the audit log of one event into a Claude-Code-style transcript.
// Live mode reads the real audit entries (WS `audit` + GET /api/audit?event_id=…); the in-browser mock
// has no audit log, so its transcript is rebuilt from the Event itself (plan, steps, gates).
// Display only: nothing here invents data. Nemotron runs with reasoning off, so a "思考" block shows
// the plan and hypotheses the model returned, never a hidden chain of thought.
import type {
  AskAnswer, AuditEntry, Event, GateDecision, Hypothesis, PlanStep, RiskLevel, StepRun, ToolName, ToolResult,
} from '../contracts'
import { hypName } from '../plain'

export type Filter = 'all' | 'tool' | 'think' | 'gate'

interface Base { key: string; ts: string; seq: number }
export interface StateItem extends Base { k: 'state'; from: string | null; to: string; reason: string }
export interface ThinkItem extends Base {
  k: 'think'; phase: string; mode: string | null; model: string | null; source: 'commander' | 'mock_fallback' | 'unknown'
  hypothesisId: string | null; causeCode: string | null; stepIds: string[]; rationale: string | null
  safeStop: boolean; error: string | null; totalMs: number | null; tokens: number | null
}
export interface ToolItem extends Base {
  k: 'tool'; tool: ToolName; stepId: string | null; args: Record<string, unknown>; attempt: number; callId: string
  status: 'running' | 'ok' | 'failed'; error: string | null; data: Record<string, unknown> | null; durationMs: number | null
  phase: string | null
}
export interface GateItem extends Base {
  k: 'gate'; stepId: string | null; tool: string | null; risk: RiskLevel | null; verdict: string | null
  reasons: string[]; checks: Record<string, boolean>; dropped: string | null
}
export interface BlockedItem extends Base { k: 'blocked'; tool: string | null; stepId: string | null; reason: string; actor: string }
export interface ApprovalItem extends Base {
  k: 'approval'; phase: 'request' | 'decision'; requestId: string; risk: string | null; round: number | null
  title: string | null; decision: string | null; optionId: string | null; reason: string | null; via: string | null; by: string
}
export interface SafetyItem extends Base { k: 'safety'; message: string; machine: string | null }
export interface AnomalyItem extends Base { k: 'anomaly'; message: string }
export interface NoteItem extends Base { k: 'note'; text: string; tone: 'verify' | 'warn' | 'plain' }
export interface AskItem extends Base { k: 'ask'; question: string; answer: AskAnswer | null; error: string | null }

export type Item = StateItem | ThinkItem | ToolItem | GateItem | BlockedItem | ApprovalItem | SafetyItem | AnomalyItem | NoteItem | AskItem

export function inFilter(it: Item, f: Filter): boolean {
  if (f === 'all') return true
  if (f === 'tool') return it.k === 'tool' || it.k === 'ask'
  if (f === 'think') return it.k === 'think' || it.k === 'state' || (it.k === 'note' && it.tone === 'verify')
  return it.k === 'gate' || it.k === 'blocked' || it.k === 'approval' || it.k === 'safety'
}

type D = Record<string, unknown>
const obj = (v: unknown): D => (v && typeof v === 'object' && !Array.isArray(v) ? (v as D) : {})
const str = (v: unknown): string | null => (v == null ? null : String(v))
const numOr = (v: unknown): number | null => (typeof v === 'number' && isFinite(v) ? v : null)

/** Every StepRun of an event, all rounds (earlier rounds first), for joining audit rows with full results. */
export function allSteps(ev: Event | null): StepRun[] {
  if (!ev) return []
  return [...(ev.prior_rounds ?? []).flatMap((r) => r.steps), ...ev.steps]
}

/** Live: audit entries (one event, any order, duplicates allowed) → transcript items, oldest first. */
export function fromAudit(entries: AuditEntry[], ev: Event | null): Item[] {
  const seen = new Set<number>()
  let sorted = entries.filter((e) => (seen.has(e.seq) ? false : (seen.add(e.seq), true))).sort((a, b) => a.seq - b.seq)
  // a scenario restart reuses event ids: keep only the latest run (from its detector wake-up on)
  let wake = -1
  sorted.forEach((e, i) => { if (e.kind === 'state_transition' && e.payload?.from == null) wake = i })
  if (wake > 0) sorted = sorted.slice(sorted[wake - 1].kind === 'anomaly' ? wake - 1 : wake)
  const out: Item[] = []
  const calls = new Map<string, ToolItem>()
  // the full result of a step (the audit keeps a brief copy: long fields are cut to "<n chars>")
  const fullResult = new Map<string, ToolResult>()
  for (const sr of allSteps(ev)) if (sr.result) fullResult.set(sr.result.call_id, sr.result)
  for (const e of sorted) {
    const p = e.payload ?? {}
    const base = { key: `a${e.seq}`, ts: e.ts, seq: e.seq }
    switch (e.kind) {
      case 'state_transition':
        out.push({ ...base, k: 'state', from: str(p.from), to: String(p.to ?? ''), reason: String(p.reason ?? p.text ?? '') })
        break
      case 'llm_call':
        out.push({
          ...base, k: 'think', phase: String(p.phase ?? ''), mode: str(p.mode), model: str(p.model),
          source: p.plan_source === 'commander' ? 'commander' : p.plan_source === 'mock_fallback' || p.fell_back_to_mock ? 'mock_fallback' : 'unknown',
          hypothesisId: str(p.hypothesis ?? p.fallback_hypothesis), causeCode: str(p.cause_code),
          stepIds: (Array.isArray(p.steps) && p.steps.length ? p.steps : Array.isArray(p.fallback_steps) ? p.fallback_steps : []).map(String),
          rationale: str(p.rationale), safeStop: p.safe_stop === true, error: str(p.error),
          totalMs: numOr(p.total_ms), tokens: numOr(p.completion_tokens),
        })
        break
      case 'tool_call': {
        const tool = e.actor.replace(/^tool:/, '') as ToolName
        const it: ToolItem = {
          ...base, k: 'tool', tool, stepId: str(p.step_id), args: obj(p.args), attempt: numOr(p.attempt) ?? 1,
          callId: String(p.call_id ?? e.seq), status: 'running', error: null, data: null, durationMs: null, phase: str(p.state),
        }
        calls.set(it.callId, it)
        out.push(it)
        break
      }
      case 'tool_result': {
        const it = calls.get(String(p.call_id))
        if (!it) break
        const full = fullResult.get(it.callId)
        it.status = p.ok ? 'ok' : 'failed'
        it.error = str(p.error)
        it.durationMs = numOr(p.duration_ms)
        it.data = full && full.ok === !!p.ok ? full.data : obj(p.data)
        break
      }
      case 'gate': {
        const d = obj(p.decision)
        out.push({
          ...base, k: 'gate', stepId: str(p.step_id ?? d.step_id), tool: str(d.tool ?? p.tool), risk: (str(d.risk) as RiskLevel | null),
          verdict: str(d.verdict) ?? (p.dropped ? 'reject' : null), reasons: Array.isArray(d.reasons) ? d.reasons.map(String) : p.reason ? [String(p.reason)] : [],
          checks: obj(d.checks) as Record<string, boolean>, dropped: str(p.dropped),
        })
        break
      }
      case 'intercepted': {
        const step = obj(p.step)
        out.push({
          ...base, k: 'blocked', tool: str(p.tool ?? step.tool ?? obj(p.decision).tool), stepId: str(p.step_id ?? step.id),
          reason: String(p.reason ?? (Array.isArray(obj(p.decision).reasons) ? (obj(p.decision).reasons as unknown[]).join('；') : '') ?? p.msg ?? ''), actor: e.actor,
        })
        break
      }
      case 'approval_request':
        out.push({
          ...base, k: 'approval', phase: 'request', requestId: String(p.id ?? ''), risk: str(p.risk), round: numOr(p.round),
          title: str(p.title), decision: null, optionId: null, reason: str(p.reason), via: null, by: e.actor,
        })
        break
      case 'approval_decision':
        out.push({
          ...base, k: 'approval', phase: 'decision', requestId: String(p.request_id ?? ''), risk: str(p.risk), round: numOr(p.round),
          title: null, decision: str(p.decision), optionId: str(p.option_id), reason: str(p.reason), via: str(p.via), by: e.actor,
        })
        break
      case 'safety_action':
        out.push({ ...base, k: 'safety', message: String(p.message ?? ''), machine: str(p.machine_id) })
        break
      case 'anomaly':
        out.push({ ...base, k: 'anomaly', message: String(p.message ?? '') })
        break
      case 'system': {
        const text = systemText(e.actor, p)
        if (text) out.push({ ...base, k: 'note', text: text.text, tone: text.tone })
        break
      }
      default:
        break
    }
  }
  return out
}

const VERDICT: Record<string, string> = { confirmed: '成立', rejected: '不成立', inconclusive: '無法驗證（安全核心已停機）' }
const STEP_STATE: Record<string, string> = { failed: '失敗', rejected: '被攔截', skipped: '未執行' }
const hypWord = hypName

/** Only the system rows a reader needs: the verifier's verdicts, failures and version rebases. */
function systemText(actor: string, p: D): { text: string; tone: NoteItem['tone'] } | null {
  if (actor === 'verifier') {
    const h = p.hypothesis ? hypWord(String(p.hypothesis)) : '判斷'
    if (Array.isArray(p.action_problems) && p.action_problems.length) {
      return { text: `程式驗證：${p.action_problems.length} 個動作未完成（${(p.action_problems as D[]).map((x) => `${x.step_id} ${STEP_STATE[String(x.state)] ?? x.state}`).join('、')}）`, tone: 'warn' }
    }
    if (p.verdict != null) {
      const v = VERDICT[String(p.verdict)] ?? String(p.verdict)
      return { text: `程式驗證 ${h}：${v}${typeof p.observed === 'number' ? `（量到 ${Math.round(p.observed * 10) / 10}）` : ''}`, tone: 'verify' }
    }
    if (p.unmeasured) return { text: `程式驗證 ${h}：此類異常沒有讀值可驗證`, tone: 'verify' }
    if (p.handed_off) return { text: `程式驗證 ${h}：已依 SOP 交現場人工處置`, tone: 'verify' }
    if (p.contained != null) return { text: `程式驗證 ${h}：${p.contained ? '機台已停用（狀態確認），交維修確認' : '機台仍未停用'}`, tone: 'verify' }
    return null
  }
  if (p.failure) return { text: `失敗：${String(p.failure)}（連續 ${String(p.consecutive_failures ?? '?')} 次）`, tone: 'warn' }
  if (p.rebased_version != null) return { text: `排程已改用第 ${String(p.rebased_version)} 版（第 ${String(p.stale_version)} 版已過期，需重新核准）`, tone: 'warn' }
  if (p.expectation_bounded) return { text: '程式把指揮官的預期值限制在安全範圍內', tone: 'plain' }
  if (p.error) return { text: String(p.error), tone: 'warn' }
  return null
}

/** Mock: no audit log in the browser, so rebuild the transcript from the event (rounds → plan, tools, gates). */
export function fromEvent(ev: Event): Item[] {
  const out: Item[] = []
  let n = 0
  const ts = ev.opened_at
  const push = (it: Omit<Item, 'seq' | 'key' | 'ts'> & { ts?: string }) => { n++; out.push({ ts, ...it, key: `m${n}`, seq: n } as Item) }
  push({ k: 'anomaly', message: ev.anomaly.message } as Omit<AnomalyItem, 'seq' | 'key' | 'ts'>)
  const rounds = [...(ev.prior_rounds ?? []).map((r) => ({ hyp: r.hypothesis_id, steps: r.steps })), { hyp: ev.plan?.hypothesis.id ?? null, steps: ev.steps }]
  for (const r of rounds) {
    if (!r.steps.length) continue
    push({ k: 'think', phase: out.some((x) => x.k === 'think') ? 'replan' : 'plan', mode: null, model: null, source: 'unknown',
      hypothesisId: r.hyp, causeCode: null, stepIds: r.steps.map((s) => s.step.id), rationale: null, safeStop: false, error: null, totalMs: null, tokens: null } as Omit<ThinkItem, 'seq' | 'key' | 'ts'>)
    for (const sr of r.steps) {
      if (sr.gate) pushGate(push, sr.step, sr.gate)
      if (sr.state === 'pending' || sr.state === 'awaiting_approval' || sr.state === 'skipped' || sr.state === 'rejected') continue
      push({ k: 'tool', tool: sr.step.tool, stepId: sr.step.id, args: sr.step.args, attempt: sr.result?.attempt ?? 1, callId: sr.result?.call_id ?? sr.step.id,
        status: sr.state === 'running' ? 'running' : sr.result?.ok ? 'ok' : 'failed', error: sr.result?.error ?? null, data: sr.result?.data ?? null,
        durationMs: sr.result?.duration_ms ?? null, phase: null } as Omit<ToolItem, 'seq' | 'key' | 'ts'>)
    }
  }
  return out
}
function pushGate(push: (it: Omit<GateItem, 'seq' | 'key' | 'ts'>) => void, step: PlanStep, g: GateDecision) {
  push({ k: 'gate', stepId: step.id, tool: step.tool, risk: g.risk, verdict: g.verdict, reasons: g.reasons, checks: g.checks, dropped: null })
}

/** Merge the 問戰情室 conversation into the event transcript by factory time. */
export function withAsks(items: Item[], asks: AskItem[]): Item[] {
  if (!asks.length) return items
  const t = (x: Item) => new Date(x.ts).getTime()
  return [...items, ...asks].sort((a, b) => t(a) - t(b) || (a.k === 'ask' ? 1 : 0) - (b.k === 'ask' ? 1 : 0) || a.seq - b.seq)
}

/** The hypothesis and planned steps a 思考 block refers to. `nth` = how many plans came before it:
 *  the n-th plan is the n-th round (prior rounds, then the current one) when the step ids agree. */
export function thinkContext(ev: Event | null, it: ThinkItem, nth: number): { hyp: Hypothesis | null; steps: PlanStep[] } {
  if (!ev) return { hyp: null, steps: [] }
  const hyp = ev.hypotheses.find((h) => h.id === it.hypothesisId) ?? null
  const rounds = [...(ev.prior_rounds ?? []).map((r) => r.steps), ev.steps].filter((r) => r.length > 0)
  const ids = (steps: StepRun[]) => steps.map((s) => s.step.id).join(',')
  const want = it.stepIds.join(',')
  const byIndex = rounds[nth]
  const match = byIndex && ids(byIndex) === want ? byIndex : [...rounds].reverse().find((r) => ids(r) === want) ?? []
  return { hyp, steps: match.map((s) => s.step) }
}

/** "預期：30 分鐘內回到 121°C 以上" */
export function expectationText(h: Hypothesis | null): string | null {
  const e = h?.expectation
  if (!e) return null
  const unit = e.channel === 'temp_c' ? '°C' : ''
  return `${e.within_min} 分鐘內${e.op === '>=' ? `回到 ${e.value}${unit} 以上` : e.op === '<=' ? `降到 ${e.value}${unit} 以下` : `維持在 ${e.value}–${e.value_hi}${unit}`}`
}
