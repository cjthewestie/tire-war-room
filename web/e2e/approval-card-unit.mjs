// Unit checks (no server, no browser) for the approval card UX (docs/research/edge-agent-harness-2026-11.md 建議 8):
// the expires_at countdown on the 主控台 plan card, the two columns (規則／程式判定 | AI 建議, stacked on the phone),
// the 討論 wording (不同意 → 討論, and no reject / 不採用 button), and 「只通知（系統未動作）」 kept off the card.
// Run: cd web && node e2e/approval-card-unit.mjs     (vite loads the TS/TSX modules directly)
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const here = dirname(fileURLToPath(import.meta.url))
let failed = 0
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }

globalThis.location ??= { search: '', hash: '', pathname: '/', protocol: 'http:', host: 'localhost' }   // transport.ts reads it at load
const vite = await createServer({ root: resolve(here, '..'), configFile: false, logLevel: 'error', appType: 'custom',
  server: { middlewareMode: true, hmr: false, ws: false }, esbuild: { jsx: 'automatic' } })
const load = (p) => vite.ssrLoadModule(p)
const { cardCountdown, cardJudge, notifyOnlyLabel, DISCUSS_HINT, DISCUSS_LABEL, PHONE_DISCUSS_HINT } = await load('/src/agent/approvalCard.ts')
const { ApprovalVisual } = await load('/src/agent/ApprovalVisual.tsx')
const { ApprovalJudge } = await load('/src/agent/ApprovalJudge.tsx')
const { WarRoomCtx } = await load('/src/warRoomCtx.ts')
const { initialState } = await load('/src/store.ts')
const React = await import('react')
const { renderToStaticMarkup } = await import('react-dom/server')

const T0 = Date.parse('2026-10-22T08:00:00+08:00')
const iso = (t) => new Date(t).toISOString()
const card = (over = {}) => ({
  id: 'AP-1', event_id: 'EV-1', step_id: 'S3', round: 1, created_at: iso(T0), risk: 'R2', title: '7 號機硫化延長 0.5 h',
  reason: '模溫偏低，延長硫化可補足交聯', done: [], gaps: [], delivery_impact: '交期不受影響', actions: ['硫化：7 號機 12 h → 12.5 h'],
  options: [], status: 'pending', decision: null, expires_at: iso(T0 + 10 * 60e3), version: 1, ...over })
const gate = (verdict, risk, checks) => ({ step_id: 'S3', tool: 'set_cure_time', verdict, risk, checks, reasons: [] })
const step = (id, tool, state, g, args = { press_id: 'CURE-07', hours: 12.5 }) => ({ step: { id, tool, args, depends_on: [], purpose: '延長硫化' }, state, result: null, gate: g })
const ev = (over = {}) => ({
  id: 'EV-1', opened_at: iso(T0), machine_id: 'CURE-07', anomaly: { message: '模溫偏低' }, state: 'await_approval', goal: null, hypotheses: [],
  plan: { goal: '', hypothesis: { id: 'H1', statement: '加熱板接觸不良，模溫低', expect: null }, rejected_hypothesis_ids: [], steps: [], alert_card: { title: '', summary: '' }, safe_stop: false, rationale: '先延長硫化保品質，再排檢修' },
  steps: [step('S1', 'read_sensors', 'ok', gate('allow', 'R0', { allow_list: true })),
    step('S3', 'set_cure_time', 'awaiting_approval', gate('needs_approval', 'R2', { allow_list: true, state_allowed: true, format_ok: true, simulation_feasible: true, evidence_ok: true }))],
  approvals: ['AP-1'], safety_actions: [], timeline: [], alert_card: null, consecutive_failures: 0, replans: 0, closed_at: null, outcome: null, ...over })

// ---------------------------------------------------------------- countdown (wall time, like the 待核准 bar)
{
  const c = card()
  const at = (min, speed = 1) => cardCountdown(c, T0 + min * 60e3, speed)
  check('2 min in: 剩 8:00', at(2)?.text === '剩 8:00' && at(2).leftS === 480 && !at(2).urgent, JSON.stringify(at(2)))
  check('119 s left: urgent', at(8 + 1 / 60)?.urgent === true)
  check('past expires_at: 已逾時, 0 s', at(11)?.text === '已逾時' && at(11).leftS === 0)
  check('10× speed: factory gap ÷ speed', cardCountdown(card({ expires_at: iso(T0 + 100 * 60e3) }), T0 + 20 * 60e3, 10)?.leftS === 480)
  check('no expires_at / decided / no clock: none', cardCountdown(card({ expires_at: null }), T0, 1) === null &&
    cardCountdown(card({ status: 'approved' }), T0, 1) === null && cardCountdown(c, NaN, 1) === null)
}

// ---------------------------------------------------------------- the two columns' content
{
  const j = cardJudge(card(), ev())
  check('rules: the card risk', j.rules.risk === 'R2')
  check('rules: only the gated step (awaiting / step_id)', j.rules.gates.length === 1 && j.rules.gates[0].stepId === 'S3', JSON.stringify(j.rules.gates.map((g) => g.stepId)))
  check('rules: verdict in words', j.rules.gates[0].verdictText === '需主管核准')
  check('rules: checks in words, 模擬可行 among them', j.rules.gates[0].checks.some((c) => c.label === '模擬可行' && c.ok) && j.rules.simFeasible === true)
  check('rules: a failed simulation check reads 不可行', cardJudge(card(), ev({ steps: [step('S3', 'set_cure_time', 'awaiting_approval', gate('reject', 'R2', { simulation_feasible: false }))] })).rules.simFeasible === false)
  check('rules: no gate record → null sim, 尚無判定紀錄', (() => { const k = cardJudge(card(), ev({ steps: [step('S3', 'set_cure_time', 'awaiting_approval', null)] })); return k.rules.simFeasible === null && k.rules.gates[0].verdictText === '尚無判定紀錄' })())
  check('ai: hypothesis, reason and rationale (model words)', j.ai.hypothesis?.includes('加熱板') && j.ai.reason?.includes('模溫偏低') && j.ai.rationale?.includes('先延長硫化'), JSON.stringify(j.ai))
  check('no event: rules still carry the risk, no gates', (() => { const k = cardJudge(card(), null); return k.rules.risk === 'R2' && k.rules.gates.length === 0 && k.ai.hypothesis === null })())
}

// ---------------------------------------------------------------- 只通知（系統未動作）: list / step rows only
{
  const closed = (steps, approvals = []) => ({ state: 'closed', approvals, steps })
  check('closed on R0 only: 只讀取（系統未動作）', notifyOnlyLabel(closed([step('S1', 'read_sensors', 'ok', gate('allow', 'R0', {}))])) === '只讀取（系統未動作）')
  check('closed on R0 + R1: 只通知（系統未動作）', notifyOnlyLabel(closed([step('S1', 'read_sensors', 'ok', gate('allow', 'R0', {})), step('S2', 'notify_teams', 'ok', gate('allow', 'R1', {}))])) === '只通知（系統未動作）')
  check('an approval / an R2 step / still open / safe stop: none',
    notifyOnlyLabel(closed([step('S1', 'read_sensors', 'ok', gate('allow', 'R0', {}))], ['AP-1'])) === null &&
    notifyOnlyLabel(closed([step('S3', 'set_cure_time', 'ok', gate('needs_approval', 'R2', {}))])) === null &&
    notifyOnlyLabel({ state: 'execute', approvals: [], steps: [step('S1', 'read_sensors', 'ok', gate('allow', 'R0', {}))] }) === null &&
    notifyOnlyLabel({ state: 'safe_stop', approvals: [], steps: [step('S1', 'read_sensors', 'ok', gate('allow', 'R0', {}))] }) === null)
}

// ---------------------------------------------------------------- the 主控台 plan card (server render)
{
  const clock = { factory_time: iso(T0 + 2 * 60e3), speed: 1, paused: false }
  const wrap = (el, c = clock) => renderToStaticMarkup(React.createElement(WarRoomCtx.Provider,
    { value: { state: { ...initialState, pending: [card()], clock: c }, transport: null, connected: true } }, el))
  const html = wrap(React.createElement(ApprovalVisual, { req: card(), ev: ev(), items: [], layout: 'plan' }))
  check('plan layout: countdown on the card', html.includes('data-testid="approve-countdown"') && html.includes('剩 8:00'), html.match(/approve-deadline[^]*?<\/span>/)?.[0]?.slice(0, 200))
  check('plan layout: two columns, rules then AI', html.includes('data-testid="approval-judge"') && html.indexOf('data-testid="judge-rules"') > 0 && html.indexOf('data-testid="judge-rules"') < html.indexOf('data-testid="judge-ai"'))
  check('plan layout: rules column carries verdict, 模擬可行 and the exact tool + args', html.includes('需主管核准') && html.includes('模擬：可行') && html.includes('set_cure_time {&quot;press_id&quot;:&quot;CURE-07&quot;,&quot;hours&quot;:12.5}'))
  check('plan layout: AI column carries the model text', html.includes('加熱板接觸不良') && html.includes('先延長硫化保品質'))
  check('plan layout: 不同意 → 討論 wording', html.includes('data-testid="discuss-hint"') && html.includes(DISCUSS_HINT) && DISCUSS_HINT.startsWith('不同意 → '))
  check('plan layout: no reject / 不採用 button', !/<button[^>]*>[^<]*(不採用|駁回|拒絕|reject)/i.test(html))
  check('plan layout: 只通知 badge not on the card', !html.includes('只通知（系統未動作）') && !html.includes('只讀取（系統未動作）'))
  const urgent = wrap(React.createElement(ApprovalVisual, { req: card(), ev: ev(), items: [], layout: 'plan' }), { ...clock, factory_time: iso(T0 + 9 * 60e3) })
  check('plan layout: under 2 min the pill turns urgent', /apv-deadline urgent/.test(urgent) && urgent.includes('剩 1:00'))
  const wide = wrap(React.createElement(ApprovalVisual, { req: card(), ev: ev(), items: [], layout: 'wide' }))
  check('wide layout: countdown too, 不同意 hint in the footer', wide.includes('data-testid="approve-countdown"') && wide.includes(DISCUSS_HINT))
  const stacked = wrap(React.createElement(ApprovalJudge, { req: card(), ev: ev(), stack: true }))
  check('phone: the two columns stack (rules above AI)', stacked.includes('apv-judge stack') && stacked.indexOf('judge-rules') < stacked.indexOf('judge-ai'))
  check('phone: 討論 button label and hint', DISCUSS_LABEL === '不同意 → 討論' && PHONE_DISCUSS_HINT.startsWith('不同意 → 按「討論」'))
}

await vite.close()
console.log(failed ? `\n${failed} FAILED` : '\nall passed')
process.exit(failed ? 1 : 0)
