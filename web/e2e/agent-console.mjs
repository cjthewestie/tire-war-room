// 主控台 Agent panel + visual approval card (issue #8), end to end against a running backend. Headless only.
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
// every chat bubble (question and answer) ends inside the transcript box
const askFits = (page) => page.evaluate(() => {
  const box = document.querySelector('[data-testid=transcript]')?.getBoundingClientRect()
  const q = [...document.querySelectorAll('[data-testid=sc-me] .sc-b')].at(-1)?.getBoundingClientRect()
  const a = [...document.querySelectorAll('[data-testid=sc-agent] .sc-b')].at(-1)?.getBoundingClientRect()
  const body = document.querySelector('[data-testid=transcript]')
  if (!box || !q || !a) return { ok: false, why: 'missing' }
  return { ok: q.right <= box.right + 0.5 && a.right <= box.right + 0.5 && body.scrollWidth <= body.clientWidth + 1,
    why: `box ${Math.round(box.right)} q ${Math.round(q.right)} a ${Math.round(a.right)} sw ${body.scrollWidth}/${body.clientWidth}` }
})
const sim = (page, body) => page.evaluate((b) => fetch('/api/sim/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) }).then((x) => x.status), body)

const b = await pw.chromium.launch({ headless: true })
try {
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, timezoneId: 'Asia/Taipei' })
  const d = await ctx.newPage()
  d.on('pageerror', (e) => errors.push(e.message))
  await d.goto(BASE + '/#agent', { waitUntil: 'networkidle' })
  await d.waitForSelector('.mode.live', { timeout: 15000 }).catch(() => {})
  check('agent tab opens on the live backend', (await d.locator('[aria-label="Agent Nemo 對話"]').count()) === 1)

  const r = [await sim(d, { action: 'scenario', scenario: 'press7_temp_drop' }), await sim(d, { action: 'speed', speed: 30 }), await sim(d, { action: 'play' })]
  check('press-7 scenario started (POST /api/sim/control)', r.every((s) => s === 200), r.join(','))
  let card = false
  for (let i = 0; i < 150 && !card; i++) { await d.waitForTimeout(1000); card = (await d.locator('[data-testid=approval-visual]').count()) > 0 }
  await sim(d, { action: 'pause' })
  check('approval reached the agent tab', card)
  await d.waitForTimeout(1500)

  // issue #37: the home panel keeps only the dialogue; every step is on the Agent page (看完整過程)
  check('home panel has no step rows', (await d.locator('[data-testid=transcript] [data-kind=tool], [data-testid=transcript] [data-kind=gate]').count()) === 0)
  await d.locator('[data-testid=see-full]').click()
  await d.waitForSelector('[data-testid=trace-body] [data-kind=tool]', { timeout: 10000 }).catch(() => {})
  const tr = d.locator('[data-testid=trace-body]')
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
  await d.locator('[data-testid=tab-agent]').click()
  await d.waitForSelector('[data-testid=transcript]')

  // home (UX brief v12): 核准區 list | 計劃板 | Agent 對話. The plan shows the board, delivery impact, who does what,
  // the option picker and only 核准 / 討論 (產線監控 links here with one line since issue #102).
  const apv = d.locator('[data-testid=plan-area] [data-testid=approval-visual]').first()
  // CJ, 2026-10-22: the S1–S5 board is not shown on the 主控台 (component and data stay); no empty gap is left behind
  check('主控台 center shows no S1–S5 board', (await apv.locator('[data-testid=plan-board]').count()) === 0 && (await apv.locator('svg[aria-label*=S1]').count()) === 0)
  const impact = await apv.locator('[data-testid=impact-table]').innerText().catch(() => '')
  check('delivery-impact table names #A-311', impact.includes('#A-311'), impact.replace(/\n/g, ' | ').slice(0, 160))
  check('核准後的執行事項 lists the actions (2 columns)', (await apv.locator('[data-testid=who-does-what] tbody tr').count()) >= 2)
  check('staffing options A and B', (await apv.locator('[data-testid=option-A]').count()) === 1 && (await apv.locator('[data-testid=option-B]').count()) === 1)
  check('one option carries the 系統建議 badge', (await apv.locator('.opt-rec').count()) === 1)
  check('plan actions are only 核准 and 討論', (await apv.locator('[data-testid=visual-discuss]').count()) === 1 && (await apv.locator('.apv-act button', { hasText: /修改|駁回/ }).count()) === 0)
  // 1440x900: the impact table is high on the screen and 核准 is pinned in view without scrolling
  const vis = await d.evaluate(() => {
    const r = (sel) => document.querySelector(sel)?.getBoundingClientRect()
    const c = r('[data-testid=plan-area] [data-testid=impact-table]'), btn = r('[data-testid=plan-area] [data-testid=visual-approve]')
    return { impactTop: Math.round(c?.top ?? 9e9), approveBottom: Math.round(btn?.bottom ?? 9e9), vh: innerHeight }
  })
  check('home: 核准 visible without scrolling', vis.approveBottom <= vis.vh, JSON.stringify(vis))
  check('one delivery line for the options (not repeated per card)', (await apv.locator('[data-testid=option-delivery]').count()) === 1)
  // 討論 puts the caret in the docked conversation in 討論這個計劃 mode
  await apv.locator('[data-testid=visual-discuss]').click()
  await d.waitForTimeout(300)
  check('討論 focuses the chat input in 討論這個計劃 mode', (await d.evaluate(() => document.activeElement?.getAttribute('data-testid'))) === 'chat-input')
  check('no duplicate element ids', (await d.evaluate(() => { const ids = [...document.querySelectorAll('[id]')].map((e) => e.id); return ids.length === new Set(ids).size })))
  await d.screenshot({ path: `${OUT}/console-agent-1440.png` })

  // 問戰情室: a read-only question gets an answer with its read steps
  await d.locator('.askbar textarea').fill('7 號機現在怎樣？')
  await d.locator('.askbar textarea').press('Enter')
  let answered = ''
  for (let i = 0; i < 60 && !answered; i++) { await d.waitForTimeout(500); const t = await d.locator('[data-testid=sc-agent]').last().innerText().catch(() => ''); answered = t.includes('思考中') ? '' : t }
  check('chat question answered', answered.length > 0, answered.slice(0, 80))
  const fit = await askFits(d)
  check('1440: question and answer stay inside the transcript', fit.ok, fit.why)
  check('plain answer: the tools collapse to one grey line (no tool rows, no labels)', (await d.locator('[data-testid=transcript] [data-kind=tool]').count()) === 0 && (await d.locator('[data-testid=sc-tools]').count()) >= 1)
  await d.screenshot({ path: `${OUT}/console-agent-ask-1440.png` })

  // issue #102: 產線監控 no longer repeats the card — one sentence, one line per approval and a button to 主控台
  await d.locator('.tabs button', { hasText: '產線監控' }).click()
  await d.waitForSelector('[data-testid=monitor-pending]', { timeout: 5000 }).catch(() => {})
  const line = await d.locator('[data-testid=monitor-pending]').innerText().catch(() => '')
  check('monitor: the approval is one line naming the machine, with 到主控台核准', line.includes('7 號硫化機') && line.includes('到主控台核准'), line.replace(/\n/g, ' | '))
  check('monitor: no approval card repeated from 主控台', (await d.locator('.monitor-wrap [data-testid=approval-visual]').count()) === 0)
  check('monitor: the sentence says how many wait for the supervisor', /要你處理 \d+ 件/.test(await d.locator('[data-testid=mon-answer]').innerText()))
  check('monitor: 硫化機排程 moved here, 11 presses', (await d.locator('[data-testid=cure-schedule] .cs-press').count()) === 11)
  await d.screenshot({ path: `${OUT}/console-monitor-1440.png`, fullPage: true })
  await d.locator('[data-testid=monitor-pending-go]').click()
  await d.waitForSelector('[data-testid=plan-area] [data-testid=approval-visual]', { timeout: 5000 }).catch(() => {})
  check('到主控台核准 opens the home tab on that card', (await d.locator('[data-testid=plan-area] [data-testid=approval-visual]').count()) >= 1)

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
  await p.locator('.askbar textarea').fill('今晚夜班缺誰？')
  await p.locator('.askbar textarea').press('Enter')
  let ans390 = ''
  for (let i = 0; i < 60 && !ans390; i++) { await p.waitForTimeout(500); const t = await p.locator('[data-testid=sc-agent]').last().innerText().catch(() => ''); ans390 = t.includes('思考中') ? '' : t }
  const fit390 = await askFits(p)
  check('390: question and answer stay inside the transcript', !!ans390 && fit390.ok, fit390.why)
  await p.locator('[data-testid=transcript]').scrollIntoViewIfNeeded()
  await p.screenshot({ path: `${OUT}/console-transcript-390.png` })
  await p.goto(BASE + '/#monitor', { waitUntil: 'networkidle' })
  await p.waitForSelector('[data-testid=monitor-pending]', { timeout: 15000 }).catch(() => {})
  await p.waitForTimeout(800)
  await p.screenshot({ path: `${OUT}/console-monitor-390.png`, fullPage: true })
  await m.close()

  // approve from the visual card (option B), confirm via GET /api/approvals
  // the press-7 card (a delivery-risk plan the 主 agent drafted on its own may also be waiting: issue #54)
  const reqId = await d.evaluate(() => fetch('/api/approvals?status=pending').then((x) => x.json()).then((a) => (a.find((r) => r.event_id.includes('CURE-07')) ?? a[0])?.id))
  await d.locator('[data-testid=plan-area] [data-testid=option-B]').first().click()
  await d.locator('[data-testid=plan-area] [data-testid=visual-approve]').first().click()
  let status = ''
  for (let i = 0; i < 20 && status !== 'approved' && status !== 'modified'; i++) {
    await d.waitForTimeout(500)
    status = await d.evaluate((id) => fetch('/api/approvals').then((x) => x.json()).then((a) => a.find((r) => r.id === id)?.status ?? ''), reqId)
  }
  const dec = await d.evaluate((id) => fetch('/api/approvals').then((x) => x.json()).then((a) => a.find((r) => r.id === id)?.decision), reqId)
  check('approved from the visual card (GET /api/approvals)', status === 'approved', `${reqId} ${status} option=${dec?.option_id} via=${dec?.via}`)
  check('decision recorded via dashboard with option B', dec?.via === 'dashboard' && dec?.option_id === 'B')

  // several approvals at once (issue #102): 產線監控 says how many, names the first, lists the others in one line
  const r3 = [await sim(d, { action: 'scenario', scenario: 'concurrent_3' }), await sim(d, { action: 'speed', speed: 30 }), await sim(d, { action: 'play' })]
  check('concurrent_3 scenario started', r3.every((x) => x === 200), r3.join(','))
  let np = 0
  for (let i = 0; i < 150 && np < 2; i++) { await d.waitForTimeout(1000); np = await d.evaluate(() => fetch('/api/approvals?status=pending').then((x) => x.json()).then((a) => a.length)) }
  await sim(d, { action: 'pause' })
  await d.locator('.tabs button', { hasText: '產線監控' }).click()
  await d.waitForTimeout(1500)
  // a third card can land between the loop above and the pause: count again with the line on screen
  np = await d.evaluate(() => fetch('/api/approvals?status=pending').then((x) => x.json()).then((a) => a.length))
  const many = await d.locator('[data-testid=monitor-pending]').innerText().catch(() => '')
  check('monitor: no card, one line with the other approvals', np >= 2 && (await d.locator('.monitor-wrap [data-testid=approval-visual]').count()) === 0 && many.includes(`另有 ${np - 1} 件`), `pending ${np}: ${many.replace(/\n/g, ' | ')}`)
  check('monitor: one row per abnormal machine', (await d.locator('[data-testid=abnormal-list] li').count()) >= np)
  const listTop = await d.evaluate(() => Math.round(document.querySelector('[data-testid=abnormal-list]')?.getBoundingClientRect().top ?? 9e9))
  check('monitor: the abnormal machines start on the first screen', listTop < 900, `${listTop}px`)
  check('no duplicate element ids (several pending)', (await d.evaluate(() => { const ids = [...document.querySelectorAll('[id]')].map((e) => e.id); return ids.length === new Set(ids).size })))
  await d.screenshot({ path: `${OUT}/console-monitor-concurrent-1440.png`, fullPage: true })
  await d.locator('[data-testid=monitor-pending-go]').click()
  await d.waitForSelector('[data-testid=plan-area] [data-testid=approval-visual]', { timeout: 5000 }).catch(() => {})
  check('到主控台核准 opens the home tab on its event', (await d.locator('[data-testid=plan-area] [data-testid=approval-visual]').count()) >= 1)
  await d.screenshot({ path: `${OUT}/console-agent-concurrent-1440.png` })
  await sim(d, { action: 'scenario', scenario: 'normal' })
  await sim(d, { action: 'play' })
  check('no page errors', errors.length === 0, errors.slice(0, 3).join(' || '))
} finally {
  await b.close()
}
console.log(`\n${results.filter(Boolean).length}/${results.length} checks passed`)
process.exit(results.every(Boolean) ? 0 : 1)
