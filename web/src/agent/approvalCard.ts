// 核准卡 UX (docs/research/edge-agent-harness-2026-11.md 建議 8): what the card shows, as pure functions so
// e2e/approval-card-unit.mjs checks it without a browser.
//   - the expires_at countdown on the card itself (the same wall-time arithmetic as the 待核准 bar)
//   - the card split in two: 規則／程式判定 (gate verdict, its checks, the exact tool + args, from the backend's own
//     fields only) next to AI 建議 (the model's words) — rules decide, the AI is a second opinion
//   - 「只通知（系統未動作）」 for events that closed on R0/R1 steps alone (shown in the list, never on the card)
//   - the 討論 wording: 不同意 → 討論 (there is no reject / 不採用 button: CJ, 「agent 計劃就只有核准跟討論」)
// Display only: nothing here sends anything.
import type { ApprovalRequest, Event, GateDecision, GateVerdict, RiskLevel, StepRun } from '../contracts'
import { URGENT_S, fmtClock } from '../approvalAlert'
import { ms } from '../format'
import { plain } from '../plain'

/** The 討論 button (phone) and the 不同意 line (desk, where 討論 is the chat box beside the card). */
export const DISCUSS_LABEL = '不同意 → 討論'
export const DISCUSS_HINT = '不同意 → 在旁邊的對話框跟 Agent Nemo 討論；未核准前不會執行'
export const PHONE_DISCUSS_HINT = '不同意 → 按「討論」跟 AI 說，AI 重排發新版；未核准前不會執行'

export interface CardCountdown {
  leftS: number          // wall seconds until the card times out (0 once past)
  text: string           // 「剩 8:00」 / 「已逾時」
  urgent: boolean        // under URGENT_S left (the bar turns red at the same point)
}

/** Countdown to expires_at, in wall seconds: expires_at is factory time, so the gap is divided by the sim speed
 *  (approvalAlert.alertModel does the same). null when the card is not waiting or has no usable expires_at / clock. */
export function cardCountdown(req: Pick<ApprovalRequest, 'status' | 'expires_at'>, nowFactory: number, speed = 1): CardCountdown | null {
  if (req.status !== 'pending' || !req.expires_at) return null
  const exp = ms(req.expires_at)
  if (!Number.isFinite(exp) || !Number.isFinite(nowFactory)) return null
  const leftS = Math.max(0, (exp - nowFactory) / 1000 / (speed > 0 ? speed : 1))
  return { leftS, text: leftS > 0 ? `剩 ${fmtClock(leftS)}` : '已逾時', urgent: leftS < URGENT_S }
}

export const VERDICT_LABEL: Record<GateVerdict, string> = { allow: '允許', needs_approval: '需主管核准', reject: '擋下' }
/** gate.py check names → plain words; an unknown check keeps its own name. */
export const CHECK_LABEL: Record<string, string> = {
  allow_list: '工具在允許清單',
  state_allowed: '此階段可用',
  format_ok: '參數格式正確',
  simulation_feasible: '模擬可行',
  evidence_ok: '與本事件數據一致',
}

export interface GateLine {
  stepId: string
  tool: string
  args: Record<string, unknown>
  verdict: GateVerdict | null
  verdictText: string
  risk: RiskLevel | null
  checks: { key: string; label: string; ok: boolean }[]
  reasons: string[]
}

export interface CardJudge {
  /** 規則／程式判定: the risk level the gate set, each gated step's verdict and checks, the simulation result */
  rules: { risk: RiskLevel; gates: GateLine[]; simFeasible: boolean | null }
  /** AI 建議: what the model wrote (title in the header; here the hypothesis, the card's reason, the plan rationale) */
  ai: { hypothesis: string | null; reason: string | null; rationale: string | null }
}

function gateLine(sr: StepRun): GateLine {
  const g: GateDecision | null = sr.gate
  return {
    stepId: sr.step.id, tool: sr.step.tool, args: sr.step.args,
    verdict: g?.verdict ?? null, verdictText: g ? VERDICT_LABEL[g.verdict] ?? g.verdict : '尚無判定紀錄',
    risk: g?.risk ?? null,
    checks: Object.entries(g?.checks ?? {}).map(([key, ok]) => ({ key, label: CHECK_LABEL[key] ?? key, ok: !!ok })),
    reasons: g?.reasons ?? [],
  }
}

/** The two columns of the card. The gated steps are the event's steps waiting for this card (state
 *  awaiting_approval, or the card's own step_id); only fields the backend already sends are used. */
export function cardJudge(req: ApprovalRequest, ev: Event | null): CardJudge {
  const steps = (ev?.steps ?? []).filter((s) => s.state === 'awaiting_approval' || (req.step_id != null && s.step.id === req.step_id))
  const gates = steps.map(gateLine)
  const sims = gates.flatMap((g) => g.checks.filter((c) => c.key === 'simulation_feasible').map((c) => c.ok))
  const hyp = ev?.plan?.hypothesis ?? null
  const text = (s: string | null | undefined) => (s && s.trim() ? plain(s) : null)
  return {
    rules: { risk: req.risk, gates, simFeasible: sims.length ? sims.every(Boolean) : null },
    ai: { hypothesis: hyp ? text(hyp.statement) : null, reason: text(req.reason), rationale: text(ev?.plan?.rationale) },
  }
}

/** 「只讀取／只通知（系統未動作）」 for an event that closed without any approval and whose every step the gate rated R0
 *  (read / compute) or R1 (fixed notice): nothing on the floor was changed. null otherwise (approvals, safe stop,
 *  still open, an R2/R3 step, a step without a gate record). For the list / step rows only, never the approval card. */
export function notifyOnlyLabel(ev: Pick<Event, 'state' | 'approvals' | 'steps'> | null | undefined): string | null {
  if (!ev || ev.state !== 'closed' || ev.approvals.length > 0 || !ev.steps.length) return null
  const risks = ev.steps.map((s) => s.gate?.risk ?? null)
  if (risks.some((r) => r !== 'R0' && r !== 'R1')) return null
  return risks.includes('R1') ? '只通知（系統未動作）' : '只讀取（系統未動作）'
}
