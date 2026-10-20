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
  const q = [...document.querySelectorAll('.r-q')].at(-1)?.getBoundingClientRect()
  const a = [...document.querySelectorAll('[data-testid=ask-answer] .r-answer')].at(-1)?.getBoundingClientRect()
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
  check('agent tab opens on the live backend', (await d.locator('[aria-label="主 agent 對話"]').count()) === 1)

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
  // the option picker and only 核准 / 討論; the evidence (Gantt compare, tools, conclusion) is on 產線監控's card.
  const apv = d.locator('[data-testid=plan-area] [data-testid=approval-visual]').first()
  const flow = await apv.locator('[data-testid=plan-board] svg').getAttribute('aria-label')
  check('plan board shows S1…S5 with status', !!flow && /S1/.test(flow) && /S5/.test(flow) && flow.includes('等主管核准'), flow?.slice(0, 120))
  const impact = await apv.locator('[data-testid=impact-table]').innerText().catch(() => '')
  check('delivery-impact table names #A-311', impact.includes('#A-311'), impact.replace(/\n/g, ' | ').slice(0, 160))
  check('核准後誰去做 lists the actions', (await apv.locator('[data-testid=who-does-what] li').count()) >= 2)
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
  for (let i = 0; i < 60 && !answered; i++) { await d.waitForTimeout(500); answered = await d.locator('[data-testid=ask-answer] .r-answer').last().innerText().catch(() => '') }
  check('chat question answered', answered.length > 0, answered.slice(0, 80))
  const fit = await askFits(d)
  check('1440: question and answer stay inside the transcript', fit.ok, fit.why)
  check('answer labelled 查詢 (its read steps are on the Agent page)', (await d.locator('[data-testid=ask-answer] .ro').innerText()) === '查詢' && (await d.locator('[data-testid=ask-answer] [data-kind=tool]').count()) === 0)
  await d.screenshot({ path: `${OUT}/console-agent-ask-1440.png` })

  // dashboard tab carries the same visual card
  await d.locator('.tabs button', { hasText: '產線監控' }).click()
  await d.waitForSelector('.monitor-wrap [data-testid=approval-visual]', { timeout: 5000 }).catch(() => {})
  check('monitor tab shows the visual card', (await d.locator('.monitor-wrap [data-testid=approval-visual]').count()) >= 1)
  const mon = d.locator('.monitor-wrap [data-testid=approval-visual]').first()
  const gantt = await mon.locator('[data-testid=schedule-compare]').innerText().catch(() => '')
  check('monitor card: before/after Gantt names #A-311', gantt.includes('#A-311'), gantt.replace(/\n/g, ' | ').slice(0, 160))
  check('monitor card: Gantt shows the cure time change', /硫化 28 h → 36\.5 h/.test(gantt))
  const chips = await mon.locator('[data-testid=tool-chips]').innerText().catch(() => '')
  check('monitor card: tools-used chips list SOP and 排程求解', chips.includes('SOP') && chips.includes('排程求解'), chips.replace(/\n/g, ' | ').slice(0, 120))
  const concl = await mon.locator('.apv-concl').innerText().catch(() => '')
  check('monitor card: conclusion why, delivery, after approval, if you disagree', ['為什麼', '交期影響', '核准後', '不同意時'].every((k) => concl.includes(k)))
  check('monitor card: plan diagram', (await mon.locator('[data-testid=plan-flow] svg').count()) === 1)
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
  await p.locator('.askbar textarea').fill('今晚夜班缺誰？')
  await p.locator('.askbar textarea').press('Enter')
  let ans390 = ''
  for (let i = 0; i < 60 && !ans390; i++) { await p.waitForTimeout(500); ans390 = await p.locator('[data-testid=ask-answer] .r-answer').last().innerText().catch(() => '') }
  const fit390 = await askFits(p)
  check('390: question and answer stay inside the transcript', !!ans390 && fit390.ok, fit390.why)
  await p.locator('[data-testid=transcript]').scrollIntoViewIfNeeded()
  await p.screenshot({ path: `${OUT}/console-transcript-390.png` })
  await p.goto(BASE + '/#monitor', { waitUntil: 'networkidle' })
  await p.waitForSelector('[data-testid=approval-visual]', { timeout: 15000 }).catch(() => {})
  await p.waitForTimeout(800)
  await p.screenshot({ path: `${OUT}/console-monitor-390.png`, fullPage: true })
  await m.close()

  // approve from the visual card (option B), confirm via GET /api/approvals
  // the press-7 card (a delivery-risk plan the 主 agent drafted on its own may also be waiting: issue #54)
  const reqId = await d.evaluate(() => fetch('/api/approvals?status=pending').then((x) => x.json()).then((a) => (a.find((r) => r.event_id.includes('CURE-07')) ?? a[0])?.id))
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

  // several approvals at once: one full card on the monitor, the rest one line each
  const r3 = [await sim(d, { action: 'scenario', scenario: 'concurrent_3' }), await sim(d, { action: 'speed', speed: 30 }), await sim(d, { action: 'play' })]
  check('concurrent_3 scenario started', r3.every((x) => x === 200), r3.join(','))
  let np = 0
  for (let i = 0; i < 150 && np < 2; i++) { await d.waitForTimeout(1000); np = await d.evaluate(() => fetch('/api/approvals?status=pending').then((x) => x.json()).then((a) => a.length)) }
  await sim(d, { action: 'pause' })
  await d.waitForTimeout(1500)
  const cards = await d.locator('.monitor-wrap [data-testid=approval-visual]').count()
  const rows = await d.locator('[data-testid=pending-more] li').count()
  check('monitor: one full card, the other approvals as one-line rows', np >= 2 && cards === 1 && rows === np - 1, `pending ${np} cards ${cards} rows ${rows}`)
  // the 預警 panel (#16) now opens 產線監控 on purpose; the check is that the approvals don't push the grid down
  const monTop = await d.evaluate(() => Math.round(document.querySelector('.monitor')?.getBoundingClientRect().top + scrollY - (((e) => (e ? e.getBoundingClientRect().height + parseFloat(getComputedStyle(e).marginBottom) : 0))(document.querySelector('[data-testid=early-warnings]')))))
  check('monitor stays within ~1.5 screens of the top (not counting 預警)', monTop < 1400, `${monTop}px`)
  check('no duplicate element ids (several pending)', (await d.evaluate(() => { const ids = [...document.querySelectorAll('[id]')].map((e) => e.id); return ids.length === new Set(ids).size })))
  await d.screenshot({ path: `${OUT}/console-monitor-concurrent-1440.png`, fullPage: true })
  await d.locator('[data-testid=pending-more] a').first().click()
  await d.waitForSelector('[data-testid=plan-area] [data-testid=approval-visual]', { timeout: 5000 }).catch(() => {})
  check('a one-line row opens the home tab on its event', (await d.locator('[data-testid=plan-area] [data-testid=approval-visual]').count()) >= 1)
  await d.screenshot({ path: `${OUT}/console-agent-concurrent-1440.png` })
  await sim(d, { action: 'scenario', scenario: 'normal' })
  await sim(d, { action: 'play' })
  check('no page errors', errors.length === 0, errors.slice(0, 3).join(' || '))
} finally {
  await b.close()
}
console.log(`\n${results.filter(Boolean).length}/${results.length} checks passed`)
process.exit(results.every(Boolean) ? 0 : 1)
