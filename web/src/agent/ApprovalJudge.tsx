// 核准卡的「規則／程式判定 | AI 建議」 two columns (docs/research/edge-agent-harness-2026-11.md 建議 8), used by the
// 主控台 plan card (ApprovalVisual, layout="plan") and stacked on the phone (Phone.tsx). Styles in index.css (.apv-judge),
// so the phone does not pull in agent.css. Display only.
import type { ApprovalRequest, Event } from '../contracts'
import { RISK_PLAIN } from '../plain'
import { TOOL_LABEL } from '../stepText'
import { cardJudge } from './approvalCard'

/** The card in two columns (stacked on the phone and on narrow screens): LEFT what the code decided — the risk level,
 *  each gated step's verdict and checks (模擬可行 among them), the exact tool and args 核准 runs; RIGHT what the AI
 *  suggests in its own words. Backend fields only; 安全核心狀態 is not sent with the card, so it is not shown. */
export function ApprovalJudge({ req, ev, stack = false }: { req: ApprovalRequest; ev: Event | null; stack?: boolean }) {
  const j = cardJudge(req, ev)
  return (
    <div className={`apv-judge ${stack ? 'stack' : ''}`} data-testid="approval-judge">
      <section className="apj-col apj-rules" aria-label="規則／程式判定" data-testid="judge-rules">
        <h5 className="apj-h">規則／程式判定</h5>
        <p className="apj-line"><span className={`apj-risk r-${j.rules.risk}`}>{j.rules.risk}</span> {RISK_PLAIN[j.rules.risk]}</p>
        {j.rules.simFeasible != null && (
          <p className={`apj-line apj-sim ${j.rules.simFeasible ? 'ok' : 'no'}`} data-testid="judge-sim">模擬：{j.rules.simFeasible ? '可行' : '不可行'}</p>
        )}
        {j.rules.gates.length
          ? (
            <ul className="apj-gates">
              {j.rules.gates.map((g) => (
                <li key={g.stepId} data-testid="judge-gate" data-verdict={g.verdict ?? ''}>
                  <span className="apj-gh"><b>{g.stepId}</b> {TOOL_LABEL[g.tool as keyof typeof TOOL_LABEL] ?? g.tool}：<span className={`apj-v v-${g.verdict ?? 'none'}`}>{g.verdictText}</span></span>
                  {g.checks.length > 0 && (
                    <span className="apj-checks">{g.checks.map((c) => <span key={c.key} className={`apj-c ${c.ok ? 'ok' : 'no'}`}>{c.ok ? '✓' : '✕'} {c.label}</span>)}</span>
                  )}
                  <code className="apj-args">{g.tool} {JSON.stringify(g.args)}</code>
                </li>
              ))}
            </ul>
          )
          : <p className="muted small">這張卡沒有附程式判定紀錄</p>}
      </section>
      <section className="apj-col apj-ai" aria-label="AI 建議" data-testid="judge-ai">
        <h5 className="apj-h">AI 建議<small>Agent Nemo 的說法</small></h5>
        {j.ai.hypothesis && <p className="apj-line">{j.ai.hypothesis}</p>}
        {j.ai.reason && <p className="apj-line">{j.ai.reason}</p>}
        {j.ai.rationale && <p className="apj-line apj-sub">{j.ai.rationale}</p>}
        {!j.ai.hypothesis && !j.ai.reason && !j.ai.rationale && <p className="muted small">AI 沒有附說明</p>}
      </section>
    </div>
  )
}
