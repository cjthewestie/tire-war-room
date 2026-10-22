// Unit checks (no server, no browser) for the Agent page's plan blocks (src/agent/transcript.ts thinkContext): each
// planned step shows its text even when the round also holds a step the code added after the plan (state_machine.py's
// 「通知維修檢查蒸汽供應（自動）」), which left the real-model H1 block with bare 「S1…S5」.
// Run: cd web && node e2e/transcript-steps-unit.mjs
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const here = dirname(fileURLToPath(import.meta.url))
let failed = 0
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }
const vite = await createServer({ root: resolve(here, '..'), configFile: false, logLevel: 'error', appType: 'custom', server: { middlewareMode: true, hmr: false, ws: false } })
const T = await vite.ssrLoadModule('/src/agent/transcript.ts')
await vite.close()

const run = (...ids) => ids.map((id) => ({ step: { id, tool: 'notify_teams', purpose: `做 ${id}`, args: {} }, state: 'done' }))
const ev = { hypotheses: [{ id: 'H1', status: 'rejected' }, { id: 'H2', status: 'active' }],
  prior_rounds: [{ hypothesis_id: 'H1', steps: run('S1', 'S2', 'S3', 'S4', 'S4b', 'S5', 'S6') }], steps: run('S1', 'S2', 'S3') }
const think = (hyp, ids) => ({ hypothesisId: hyp, stepIds: ids })
const h1 = T.thinkContext(ev, think('H1', ['S1', 'S2', 'S3', 'S4', 'S4b', 'S5']), 0)
check('H1: every planned step has its text (round also has the auto S6)', h1.steps.length === 6 && h1.steps.every((s) => s.purpose), h1.steps.map((s) => s.id).join(','))
check('H1: the plan\'s order, not the auto step', h1.steps.map((s) => s.id).join(',') === 'S1,S2,S3,S4,S4b,S5')
const h2 = T.thinkContext(ev, think('H2', ['S1', 'S2', 'S3']), 1)
check('H2: its own round', h2.steps.length === 3 && h2.steps[0].purpose === '做 S1' && h2.hyp?.id === 'H2')
const h2late = T.thinkContext(ev, think('H2', ['S1', 'S2', 'S3']), 5)
check('no round by index: the latest round that holds the steps (H2, not H1)', h2late.steps.length === 3 && ev.steps.some((s) => s.step === h2late.steps[0]))
check('steps no round holds: none (the block falls back to the ids)', T.thinkContext(ev, think('H3', ['S9']), 2).steps.length === 0)
console.log(failed ? `\n${failed} FAILED` : '\nall passed')
process.exit(failed ? 1 : 0)
