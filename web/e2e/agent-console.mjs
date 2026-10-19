// Agent 對話台 + visual approval card (issue #8), end to end against a running backend. Headless only.
//   PORT=8047 ./start.sh   then   node web/e2e/agent-console.mjs http://localhost:8047
// Starts the press-7 scenario, waits for the approval, checks the console transcript (sop_search and
// solve_schedule tool rows, the plan block), the visual card (plan diagram, before/after Gantt with
// #A-311, options A/B, tools, conclusion), asks a question, approves from the card and confirms the
// decision via GET /api/approvals. Writes web/e2e/screenshots/console-*.png at 1440 and 390 wide.
import { createRequire } from 'node:module'
import { mkdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
let pw
for (const id of ['playwright', 'playwright-core', `${homedir()}/.local/lib/node_modules/@playwright/cli/node_modules/playwright-core`]) { try { pw = require(id); break } catch { /* next */ } }
if (!pw) { console.error('playwright not found (npm i -g @playwright/cli or npx playwright install chromium)'); process.exit(2) }
const BASE = process.argv[2] ?? 'http://localhost:8000'
const OUT = resolve(dirname(fileURLToPath(import.meta.url)), 'screenshots')
mkdirSync(OUT, { recursive: true })
const results = []
const check = (n, ok, d = '') => { results.push(!!ok); console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${d ? `  — ${d}` : ''}`) }
const errors = []
const sim = (page, body) => page.evaluate((b) => fetch('/api/sim/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) }).then((x) => x.status), body)

const b = await pw.chromium.launch({ headless: true })
try {
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, timezoneId: 'Asia/Taipei' })
  const d = await ctx.newPage()
  d.on('pageerror', (e) => errors.push(e.message))
  await d.goto(BASE + '/#agent', { waitUntil: 'networkidle' })
  await d.waitForSelector('.mode.live', { timeout: 15000 }).catch(() => {})
  check('agent tab opens on the live backend', (await d.locator('[aria-label="Agent 對話台"]').count()) === 1)

  const r = [await sim(d, { action: 'scenario', scenario: 'press7_temp_drop' }), await sim(d, { action: 'speed', speed: 30 }), await sim(d, { action: 'play' })]
  check('press-7 scenario started (POST /api/sim/control)', r.every((s) => s === 200), r.join(','))
  let card = false
  for (let i = 0; i < 150 && !card; i++) { await d.waitForTimeout(1000); card = (await d.locator('[data-testid=approval-visual]').count()) > 0 }
  await sim(d, { action: 'pause' })
  check('approval reached the agent tab', card)
  await d.waitForTimeout(1500)

  const tr = d.locator('[data-testid=transcript]')
  const nSop = await tr.locator('[data-kind=tool][data-tool=sop_search]').count()
  const nSolve = await tr.locator('[data-kind=tool][data-tool=solve_schedule]').count()
  check('console shows ≥1 sop_search tool row', nSop >= 1, `${nSop}`)
  check('console shows ≥1 solve_schedule tool row', nSolve >= 1, `${nSolve}`)
  check('console shows the plan the commander returned', (await tr.locator('[data-testid=think-block]').count()) >= 1)
  check('console shows a gate row', (await tr.locator('[data-kind=gate]').count()) >= 1)
  // a tool row expands to its args and raw JSON
  await tr.locator('[data-kind=tool][data-tool=solve_schedule] > summary').first().click()
  await tr.locator('[data-kind=tool][data-tool=solve_schedule] .r-json > details > summary').first().click()
  check('tool row expands to its args JSON', (await tr.locator('[data-kind=tool][data-tool=solve_schedule] pre').first().innerText()).includes('cure_overrides_h'))

  const apv = d.locator('[data-testid=approval-visual]').first()
  const flow = await apv.locator('[data-testid=plan-flow] svg').getAttribute('aria-label')
  check('plan diagram shows S1…S5 with status', !!flow && /S1/.test(flow) && /S5/.test(flow) && flow.includes('等主管核准'), flow?.slice(0, 120))
  const gantt = await apv.locator('[data-testid=schedule-compare]').innerText().catch(() => '')
  check('before/after Gantt names #A-311', gantt.includes('#A-311'), gantt.replace(/\n/g, ' | ').slice(0, 160))
  check('Gantt shows the cure time change', /硫化 28 h → 36\.5 h/.test(gantt))
  check('staffing options A and B', (await apv.locator('[data-testid=option-A]').count()) === 1 && (await apv.locator('[data-testid=option-B]').count()) === 1)
  check('one option carries the 系統建議 badge', (await apv.locator('.opt-rec').count()) === 1)
  const chips = await apv.locator('[data-testid=tool-chips]').innerText()
  check('tools-used chips list SOP and 排程求解', chips.includes('SOP') && chips.includes('排程求解'), chips.replace(/\n/g, ' | ').slice(0, 120))
  const concl = await apv.locator('.apv-concl').innerText()
  check('conclusion: why, delivery, approve vs reject', ['為什麼', '交期影響', '核准後', '駁回後'].every((k) => concl.includes(k)))
  await d.screenshot({ path: `${OUT}/console-agent-1440.png` })

  // 問戰情室: a read-only question gets an answer with its read steps
  await d.locator('.askbar textarea').fill('7 號機現在怎樣？')
  await d.locator('.askbar textarea').press('Enter')
  let answered = ''
  for (let i = 0; i < 60 && !answered; i++) { await d.waitForTimeout(500); answered = await d.locator('[data-testid=ask-answer] .r-answer').last().innerText().catch(() => '') }
  check('chat question answered', answered.length > 0, answered.slice(0, 80))
  check('answer labelled 只查不改 and shows its read steps', (await d.locator('[data-testid=ask-answer] .ro').count()) > 0 && (await d.locator('[data-testid=ask-answer] [data-kind=tool]').count()) > 0)
  await d.screenshot({ path: `${OUT}/console-agent-ask-1440.png` })

  // dashboard tab carries the same visual card
  await d.locator('.tabs button', { hasText: '產線監控' }).click()
  await d.waitForSelector('.monitor-wrap [data-testid=approval-visual]', { timeout: 5000 }).catch(() => {})
  check('monitor tab shows the visual card', (await d.locator('.monitor-wrap [data-testid=approval-visual]').count()) >= 1)
  await d.screenshot({ path: `${OUT}/console-monitor-1440.png`, fullPage: true })

  // 390 wide
  const m = await b.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, timezoneId: 'Asia/Taipei', isMobile: true, hasTouch: true })
  const p = await m.newPage()
  p.on('pageerror', (e) => errors.push(e.message))
  await p.goto(BASE + '/#agent', { waitUntil: 'networkidle' })
  await p.waitForSelector('[data-testid=approval-visual]', { timeout: 15000 }).catch(() => {})
  await p.waitForTimeout(1200)
  const overflow = await p.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  check('390: no horizontal page scroll', overflow <= 1, `${overflow}px`)
  await p.screenshot({ path: `${OUT}/console-agent-390.png`, fullPage: true })
  await p.locator('[data-testid=transcript]').scrollIntoViewIfNeeded()
  await p.screenshot({ path: `${OUT}/console-transcript-390.png` })
  await p.goto(BASE + '/', { waitUntil: 'networkidle' })
  await p.waitForSelector('[data-testid=approval-visual]', { timeout: 15000 }).catch(() => {})
  await p.waitForTimeout(800)
  await p.screenshot({ path: `${OUT}/console-monitor-390.png`, fullPage: true })
  await m.close()

  // approve from the visual card (option B), confirm via GET /api/approvals
  const reqId = await d.evaluate(() => fetch('/api/approvals?status=pending').then((x) => x.json()).then((a) => a[0]?.id))
  await d.locator('.monitor-wrap [data-testid=option-B]').first().click()
  await d.locator('.monitor-wrap [data-testid=visual-approve]').first().click()
  let status = ''
  for (let i = 0; i < 20 && status !== 'approved' && status !== 'modified'; i++) {
    await d.waitForTimeout(500)
    status = await d.evaluate((id) => fetch('/api/approvals').then((x) => x.json()).then((a) => a.find((r) => r.id === id)?.status ?? ''), reqId)
  }
  const dec = await d.evaluate((id) => fetch('/api/approvals').then((x) => x.json()).then((a) => a.find((r) => r.id === id)?.decision), reqId)
  check('approved from the visual card (GET /api/approvals)', status === 'approved', `${reqId} ${status} option=${dec?.option_id} via=${dec?.via}`)
  check('decision recorded via dashboard with option B', dec?.via === 'dashboard' && dec?.option_id === 'B')
  await sim(d, { action: 'play' })
  check('no page errors', errors.length === 0, errors.slice(0, 3).join(' || '))
} finally {
  await b.close()
}
console.log(`\n${results.filter(Boolean).length}/${results.length} checks passed`)
process.exit(results.every(Boolean) ? 0 : 1)
