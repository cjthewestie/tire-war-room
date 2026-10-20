// Issue #52 check against a live backend serving web/dist (headless):
//   PORT=8103 LLM_MODE=mock uv run python -m warroom.api   then   node web/e2e/chat52.mjs http://localhost:8103 [outDir]
// The header link chip (連線中 / 模型未連線 / 離線・示範資料 / 重新連線…), the Agent page input bar sharing one
// conversation with 主控台 (「9 號機今晚要保養」 sent on the Agent page → v2 card on 主控台), and the phone 「更多」 menu.
// Screenshots at 1440 and 390, light and dark, go to outDir (default docs/screenshots/issue-52).
import { createRequire } from 'node:module'
import { homedir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { mkdirSync } from 'node:fs'

const require = createRequire(import.meta.url)
let pw
for (const id of ['playwright', 'playwright-core', `${homedir()}/.local/lib/node_modules/@playwright/cli/node_modules/playwright-core`]) { try { pw = require(id); break } catch { /* next */ } }
const BASE = process.argv[2] ?? 'http://localhost:8103'
const OUT = process.argv[3] ?? resolve(dirname(fileURLToPath(import.meta.url)), '../../docs/screenshots/issue-52')
mkdirSync(OUT, { recursive: true })
const results = []
const check = (n, ok, d = '') => { results.push(ok); console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${d ? `  — ${d}` : ''}`) }
const errors = []
const b = await pw.chromium.launch({ headless: true })
const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, timezoneId: 'Asia/Taipei' })
const p = await ctx.newPage()
p.on('pageerror', (e) => errors.push(e.message))
const post = (path, body) => p.evaluate(async ([u, bd]) => (await fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(bd) })).status, [path, body])
const until = async (f, n = 60) => { for (let i = 0; i < n; i++) { if (await f()) return true; await p.waitForTimeout(500) } return false }

// 1. link chip
await p.goto(BASE + '/', { waitUntil: 'networkidle' })
await p.waitForSelector('[data-testid="link-chip"].live', { timeout: 15000 })
const health = await p.evaluate(() => fetch('/api/health').then((r) => r.json()))
const chip = () => p.locator('[data-testid="link-chip"]')
const want = health.llm_mode === 'llm' && health.llm_reachable ? '連線中' : '模型未連線'
check(`chip says ${want} on the live backend (${health.llm_mode})`, (await chip().innerText()) === want, await chip().innerText())
check('no 即時・LLM 指揮官 text any more', !(await p.locator('header.topbar').innerText()).includes('即時・'))
check('mode details are in the chip title (後台 → 狀態)', ((await chip().getAttribute('title')) ?? '').includes('後台'))
// the backend's WebSocket refuses (a second page, so the main one stays connected): the transport keeps retrying
{
  const r = await ctx.newPage()
  let block = true
  await r.routeWebSocket(/\/ws$/, (ws) => { if (block) ws.close(); else ws.connectToServer() })
  await r.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
  const rc = () => r.locator('[data-testid="link-chip"]')
  const waitFor = async (f, n) => { for (let i = 0; i < n; i++) { if (await f()) return true; await r.waitForTimeout(500) } return false }
  check('chip says 重新連線… while the WebSocket reconnects', await waitFor(async () => (await rc().innerText()) === '重新連線…', 30), await rc().innerText())
  await r.screenshot({ path: `${OUT}/chip-reconnect-1440.png`, clip: { x: 900, y: 0, width: 540, height: 70 } })
  block = false
  check('chip comes back after reconnecting', await waitFor(async () => (await rc().innerText()) === want, 40), await rc().innerText())
  await r.close()
}
{
  const m = await b.newPage({ viewport: { width: 1440, height: 900 } })
  await m.goto(BASE + '/?mock=1', { waitUntil: 'networkidle' })
  await m.waitForTimeout(800)
  const t = await m.locator('[data-testid="link-chip"]').innerText()
  check('chip says 離線・示範資料 on the in-browser mock', t === '離線・示範資料', t)
  await m.screenshot({ path: `${OUT}/chip-mock-1440.png`, clip: { x: 900, y: 0, width: 540, height: 70 } })
  await m.close()
}
await p.screenshot({ path: `${OUT}/chip-live-1440.png`, clip: { x: 900, y: 0, width: 540, height: 70 } })

// 2. Agent page chat: the press-7 card, then 「9 號機今晚要保養」 sent from the Agent page
await post('/api/sim/control', { action: 'scenario', scenario: 'press7_temp_drop' })
for (let i = 0; i < 12; i++) {
  await post('/api/sim/step', { seconds: 120 })
  if (await p.evaluate(() => fetch('/api/approvals?status=pending').then((r) => r.json()).then((a) => a.length > 0))) break
}
const ev = await p.evaluate(() => fetch('/api/approvals?status=pending').then((r) => r.json()).then((a) => a[0]?.event_id))
check('press-7 approval card waiting', !!ev, ev)
await p.goto(`${BASE}/#console?event=${encodeURIComponent(ev)}`, { waitUntil: 'networkidle' })
await p.waitForSelector('[data-testid="trace-chat"] [data-testid="chat-input"]')
check('Agent page has the 主 agent input (one input, no mode switch)', await p.locator('[data-testid="trace-chat"] [role="radio"]').count() === 0 && await p.locator('[data-testid="trace-chat"] [data-testid="chat-input"]').count() === 1)
check('it knows the selected waiting plan', await p.locator('[data-testid="trace-chat"] [data-testid="chat-input"][data-plan="waiting"]').count() === 1)
await p.locator('[data-testid="trace-chat"] [data-testid="chat-input"]').fill('9 號機今晚要保養')
await p.locator('[data-testid="trace-chat"] [data-testid="chat-input"]').press('Enter')
for (let i = 0; i < 6; i++) await post('/api/sim/step', { seconds: 30 })
const tb = p.locator('[data-testid="trace-body"]')
check('Agent page transcript shows the message', await until(async () => (await tb.locator('[data-testid="chat-me"]', { hasText: '9 號機今晚要保養' }).count()) >= 1))
check('…and the steps behind it live (replan, card v1 → v2)', await until(async () => (await tb.locator('[data-kind="state"][data-to="replan"]').count()) >= 1 && (await tb.locator('[data-testid="card-version"]').count()) >= 1),
  (await tb.locator('[data-testid="card-version"]').last().innerText().catch(() => '')).replace(/\n/g, ' | ').slice(0, 120))
await p.screenshot({ path: `${OUT}/agent-chat-1440-light.png`, fullPage: false })
await p.click('[data-testid="tab-agent"]')
await p.waitForSelector('[data-testid="plan-area"]')
const side = p.locator('[data-testid="plan-area"]')
check('主控台 shows the v2 card', await until(async () => (await side.locator('[data-testid="card-version-tag"]').innerText().catch(() => '')) === 'v2', 60))
check('主控台 shows the same message (one 主 agent thread)', await until(async () => (await p.locator('[data-testid="transcript"] [data-testid="sc-me"]', { hasText: '9 號機今晚要保養' }).count()) >= 1))
// and back: an ask typed on 主控台 shows on the Agent page
await p.locator('[data-testid="chat-input"]').fill('9 號機下一模是哪張訂單？')
await p.locator('[data-testid="chat-input"]').press('Enter')
await until(async () => !(await p.locator('[data-testid="sc-agent"]').last().innerText()).includes('思考中'), 40)
await p.click('[data-testid="tab-console"]')
await p.click('[data-testid="tree-root"]')
check('a question from 主控台 shows in the Agent page 主 agent thread', await until(async () => (await p.locator('[data-testid="main-thread"] [data-testid="sc-me"]', { hasText: '9 號機下一模' }).count()) >= 1, 20))

// 3. phones: 7 nav items, 狀態機 and 後台 under 「更多」
{
  const c = await b.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })
  const m = await c.newPage()
  m.on('pageerror', (e) => errors.push(`390: ${e.message}`))
  await m.goto(BASE + '/', { waitUntil: 'networkidle' })
  const labels = await m.locator('.bottomnav button').allInnerTexts()
  check('390: bottom nav has 7 items', labels.length === 7, labels.join('|'))
  const wraps = await m.evaluate(() => [...document.querySelectorAll('.bottomnav button')].filter((b) => b.scrollWidth > b.clientWidth + 1).length)
  check('390: no nav label is cut or wraps', wraps === 0)
  await m.tap('[data-testid="nav-more"]')
  await m.waitForSelector('[data-testid="more-sheet"]')
  check('390: 更多 opens 狀態機 and 後台', (await m.locator('[data-testid="more-sheet"]').innerText()).includes('狀態機') && (await m.locator('[data-testid="more-sheet"]').innerText()).includes('後台'))
  await m.screenshot({ path: `${OUT}/more-390-light.png` })
  await m.locator('[data-testid="more-sheet"] button', { hasText: '後台' }).tap()
  await m.waitForSelector('[data-testid="admin-page"]')
  check('390: 後台 opens from 更多 and the nav button names it', (await m.locator('[data-testid="nav-more"]').innerText()).includes('後台'))
  await c.close()
}

// screenshots: Agent page with the input bar and the header chip, 1440 / 390, light / dark
for (const theme of ['light', 'dark']) {
  for (const [w, h] of [[1440, 900], [390, 844]]) {
    const c = await b.newContext({ viewport: { width: w, height: h }, isMobile: w < 500, hasTouch: w < 500 })
    await c.addInitScript((t) => { try { localStorage.setItem('warroom-theme', t) } catch { /* */ } }, theme)
    const s = await c.newPage()
    s.on('pageerror', (e) => errors.push(`${w}/${theme}: ${e.message}`))
    for (const [name, path] of [['agent', '/'], ['console', `/#console?event=${encodeURIComponent(ev)}`]]) {
      await s.goto(BASE + path, { waitUntil: 'networkidle' })
      await s.waitForTimeout(1200)
      const over = await s.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
      if (w < 500) check(`390 no sideways scroll ${name} ${theme}`, over <= 1, `${over}px`)
      await s.screenshot({ path: `${OUT}/${name}-${w}-${theme}.png`, fullPage: w >= 500 && name === 'console' })
    }
    await c.close()
  }
}
check('no page errors', errors.length === 0, errors.slice(0, 3).join(' | '))
await b.close()
const ok = results.every(Boolean)
console.log(ok ? `ALL PASS (${results.length})` : `${results.filter((x) => !x).length} FAILED of ${results.length}`)
process.exit(ok ? 0 : 1)
