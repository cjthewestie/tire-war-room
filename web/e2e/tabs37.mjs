// Issue #37 check against a live backend serving web/dist (headless):
//   PORT=8103 LLM_MODE=mock uv run python -m warroom.api   then   node web/e2e/tabs37.mjs http://localhost:8103 [outDir]
// Merged 產線監控, the 訂單 list, the 人力排程 explainer, the home dialogue-only panel, the Agent page and 後台;
// screenshots of every tab at 1440 and 390, light and dark, go to outDir (default docs/screenshots/issue-37).
import { createRequire } from 'node:module'
import { homedir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { mkdirSync } from 'node:fs'

const require = createRequire(import.meta.url)
let pw
for (const id of ['playwright', 'playwright-core', `${homedir()}/.local/lib/node_modules/@playwright/cli/node_modules/playwright-core`]) { try { pw = require(id); break } catch { /* next */ } }
const BASE = process.argv[2] ?? 'http://localhost:8103'
const OUT = process.argv[3] ?? resolve(dirname(fileURLToPath(import.meta.url)), '../../docs/screenshots/issue-37')
mkdirSync(OUT, { recursive: true })
const results = []
const check = (n, ok, d = '') => { results.push(ok); console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${d ? `  — ${d}` : ''}`) }
const errors = []

const b = await pw.chromium.launch({ headless: true })
const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, timezoneId: 'Asia/Taipei' })
const p = await ctx.newPage()
p.on('pageerror', (e) => errors.push(e.message))
await p.goto(BASE + '/#monitor', { waitUntil: 'networkidle' })

// an event with an approval, so every page has something to show
const post = (path, body) => p.evaluate(async ([u, bd]) => (await fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(bd) })).status, [path, body])
await post('/api/sim/control', { action: 'scenario', scenario: 'press7_temp_drop' })
for (let i = 0; i < 12; i++) {   // the scenario loads paused at 02:12; step until the approval card is out
  await post('/api/sim/step', { seconds: 120 })
  if (await p.evaluate(() => fetch('/api/approvals?status=pending').then((r) => r.json()).then((a) => a.length > 0))) break
}
await p.evaluate(() => fetch('/api/ask', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question: '7 號機現在怎樣？' }) }))

// 1. merged 產線監控
await p.goto(BASE + '/#world', { waitUntil: 'networkidle' })
await p.waitForSelector('[data-testid="plant-panels"]', { timeout: 15000 }).catch(() => {})
check('#world opens the merged 產線監控', await p.locator('[data-testid="tab-monitor"][aria-selected="true"]').count() === 1)
check('no 工廠全貌 tab', await p.locator('.tabs button', { hasText: '工廠全貌' }).count() === 0)
check('預警 first', await p.evaluate(() => document.querySelector('main')?.firstElementChild?.getAttribute('data-testid') === 'early-warnings'))
check('28 machines', await p.locator('.machines [data-machine]').count() === 28, String(await p.locator('.machines [data-machine]').count()))
for (const id of ['plant-panels', 'people-zones', 'materials', 'dock-trucks', 'world-goods', 'order-pipeline', 'kpi-people']) {
  check(`monitor has ${id}`, await p.locator(`[data-testid="${id}"]`).count() >= 1)
}
check('one order pipeline only', await p.locator('[data-testid="order-pipeline"]').count() === 1)

// 2. 訂單
await p.click('[data-testid="tab-orders"]')
await p.waitForSelector('[data-testid="order-list"] .ot-row')
const nOrders = await p.evaluate(() => fetch('/api/world').then((r) => r.json()).then((w) => w.orders.items.length))
check('every order listed', await p.locator('[data-testid="order-list"] .ot-row').count() === nOrders, `${nOrders}`)
const ranks = await p.locator('[data-testid="order-list"] .ot-row').evaluateAll((els) => els.map((e) => ['r-late', 'r-risk', 'r-ok', 'r-done'].findIndex((c) => e.classList.contains(c))))
check('at-risk first', ranks.every((r, i) => i === 0 || ranks[i - 1] <= r), ranks.join(''))
check('rush marked', await p.locator('[data-testid="order-list"] .tag.rush').count() >= 1)
await p.fill('[data-testid="order-search"]', 'A-311')
check('search filters', await p.locator('[data-testid="order-list"] .ot-row').count() === 1 && await p.locator('[data-testid="order-card"]').count() === 1)
await p.fill('[data-testid="order-search"]', '')

// 3. 人力排程
await p.click('.tabs button:has-text("人力排程")')
await p.waitForSelector('.sched-help')
const help = await p.locator('.sched-help').innerText()
check('schedule explains order ids and the bottleneck', help.includes('訂單編號') && help.includes('瓶頸') && help.includes('11 台硫化機'))
check('schedule lists next mold-ins in words', await p.locator('.sched-next tbody tr').count() >= 1)

// 4. home: dialogue only + link
await p.click('[data-testid="tab-agent"]')
await p.waitForSelector('[data-testid="transcript"]')
await p.waitForTimeout(800)
check('home panel has no tool / think rows', await p.locator('[data-testid="transcript"] [data-kind="tool"], [data-testid="transcript"] [data-kind="think"], [data-testid="transcript"] [data-kind="gate"]').count() === 0)
check('home panel links 看完整過程', await p.locator('[data-testid="see-full"]').count() === 1)

// 5. Agent page
await p.click('[data-testid="see-full"]')
await p.waitForSelector('[data-testid="agent-page"]')
await p.waitForSelector('[data-testid="trace-body"] [data-kind="tool"]', { timeout: 10000 }).catch(() => {})
check('Agent page shows tool calls', await p.locator('[data-testid="trace-body"] [data-kind="tool"]').count() >= 1)
check('Agent page shows plan + state + gate', await p.locator('[data-testid="trace-body"] [data-kind="think"]').count() >= 1 &&
  await p.locator('[data-testid="trace-body"] [data-kind="state"]').count() >= 1 && await p.locator('[data-testid="trace-body"] [data-kind="gate"]').count() >= 1)
check('Agent page lists data read (SOP)', (await p.locator('.trace-data').innerText()).includes('SOP'))
await p.click('[data-testid="trace-filter-data"]')
const kinds = await p.locator('[data-testid="trace-body"] > *').evaluateAll((els) => [...new Set(els.map((e) => e.getAttribute('data-kind')))])
check('讀資料 filter shows only tool rows', kinds.length === 1 && kinds[0] === 'tool', kinds.join(','))
await p.click('[data-testid="trace-filter-all"]')

// 6. 後台
await p.click('[data-testid="tab-admin"]')
await p.waitForSelector('[data-testid="audit-chain"]')
check('admin status: audit chain ok', (await p.locator('[data-testid="audit-chain"]').innerText()).includes('完整'))
await p.click('[data-testid="admin-rag"]')
await p.waitForSelector('[data-testid="sop-docs"] tbody tr')
check('admin RAG lists SOP files', await p.locator('[data-testid="sop-docs"] tbody tr').count() >= 15)
await p.fill('[data-testid="rag-q"]', '硫化中斷處置')
await p.press('[data-testid="rag-q"]', 'Enter')
await p.waitForSelector('[data-testid="rag-hits"] li', { timeout: 30000 })
check('admin search tester shows hits with scores', await p.locator('[data-testid="rag-hits"] .hit-score').count() >= 1)
await p.click('[data-testid="admin-memory"]')
await p.waitForSelector('[data-testid="memory-groups"] .mem-g')
check('admin memory shows the 8 past incidents', await p.locator('.k-inc').count() === 8, String(await p.locator('.k-inc').count()))
await p.click('[data-testid="admin-runs"]')
await p.waitForSelector('[data-testid="runs"] tbody tr')
check('admin runs lists events and asks', (await p.locator('[data-testid="runs"]').innerText()).includes('事件') && (await p.locator('[data-testid="runs"]').innerText()).includes('提問'))

// screenshots: every tab, 1440 and 390, light and dark
const TABS = [['agent', '/'], ['monitor', '/#monitor'], ['orders', '/#orders'], ['schedule', '/#schedule'], ['console', '/#console'], ['admin-rag', '/#admin/rag'], ['admin-memory', '/#admin/memory'], ['admin-status', '/#admin/status']]
for (const theme of ['light', 'dark']) {
  for (const [w, h] of [[1440, 900], [390, 844]]) {
    const c = await b.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: 1, timezoneId: 'Asia/Taipei', isMobile: w < 500, hasTouch: w < 500 })
    await c.addInitScript((t) => { try { localStorage.setItem('warroom-theme', t) } catch { /* */ } }, theme)
    const s = await c.newPage()
    s.on('pageerror', (e) => errors.push(`${w}/${theme}: ${e.message}`))
    for (const [name, path] of TABS) {
      if (w < 500 && !['agent', 'monitor', 'orders', 'console', 'admin-rag'].includes(name)) continue
      await s.goto(BASE + path, { waitUntil: 'networkidle' })
      await s.waitForTimeout(name.startsWith('admin') ? 1500 : 900)
      if (name === 'admin-rag') { await s.fill('[data-testid="rag-q"]', '蒸汽壓力低'); await s.press('[data-testid="rag-q"]', 'Enter'); await s.waitForSelector('[data-testid="rag-hits"] li', { timeout: 20000 }).catch(() => {}) }
      await s.evaluate(() => window.scrollTo(0, 0))
      const overflow = await s.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
      if (w < 500) check(`no sideways scroll ${name} 390 ${theme}`, overflow <= 1, `${overflow}px`)
      await s.screenshot({ path: `${OUT}/${name}-${w}-${theme}.png`, fullPage: w >= 500 })
    }
    await c.close()
  }
}
check('no page errors', errors.length === 0, errors.slice(0, 3).join(' | '))
await b.close()
const ok = results.every(Boolean)
console.log(ok ? `ALL PASS (${results.length})` : `${results.filter((x) => !x).length} FAILED of ${results.length}`)
process.exit(ok ? 0 : 1)
