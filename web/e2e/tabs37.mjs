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
const post = (path, body) => p.evaluate(async ([u, bd]) => (await fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(bd) })).status, [path, body])

// (a) 主控台 rename
check('home tab is 主控台', (await p.locator('[data-testid="tab-agent"]').innerText()).trim() === '主控台')
check('no 對話台 anywhere in the tabs', (await p.locator('.tabs').innerText()).indexOf('對話台') < 0)

// (c) ticker: fixed counts left, scrolling items right
await p.goto(BASE + '/', { waitUntil: 'networkidle' })
await p.waitForSelector('[data-testid="tk-track"] .tk-i')
const fixed = await p.locator('[data-testid="tk-fixed"]').innerText()
check('ticker: fixed 正常 / 異常 / 待核准', fixed.includes('正常') && fixed.includes('異常') && fixed.includes('待核准'), fixed.replace(/\n/g, ' '))
const ticks = await p.locator('[data-testid="tk-track"] > .tk-i').allInnerTexts()
check('ticker: per-line, press and plant items', ticks.some((t) => /A 線/.test(t)) && ticks.some((t) => /號硫化機/.test(t)) && ticks.length >= 12, `${ticks.length}: ${ticks.slice(0, 4).join(' | ')}`)
check('ticker scrolls (animation running)', await p.evaluate(() => getComputedStyle(document.querySelector('[data-testid="tk-track"]')).animationName === 'tk-scroll'))
await p.hover('.tk-run', { position: { x: 400, y: 10 } })
check('ticker pauses on hover', await p.evaluate(() => getComputedStyle(document.querySelector('[data-testid="tk-track"]')).animationPlayState === 'paused'))
const pressTick = p.locator('[data-testid="tk-track"] > .tk-i[data-tick^="m-CURE"]').first()
await pressTick.click({ force: true })
await p.waitForSelector('[data-testid="tab-monitor"][aria-selected="true"]')
check('ticker item opens 產線監控 on that machine', (await p.locator('.drawer, [role="dialog"]').count()) >= 1 || (await p.locator('.flash').count()) >= 1)
await p.keyboard.press('Escape')

// (b) ② delivery risk (issue #54): the 主 agent drafts a plan by itself → 主 agent 規劃中… → its card is selected with 核准並派發
// (the 'normal' scenario: the day-loop default starts at 00:00, where a replan may have nothing to change)
await post('/api/sim/control', { action: 'scenario', scenario: 'normal' })
await p.goto(BASE + '/', { waitUntil: 'networkidle' })
await p.waitForSelector('[data-testid^="home-item-order:"]')
const riskItem = p.locator('[data-testid^="home-item-order:"]').first()
check('② items say the 主 agent will draft the plan (or is drafting it)', (await p.locator('[data-testid="item-replan-hint"], [data-testid="item-planning"]').count()) >= 1)
await riskItem.click()
await p.waitForSelector('[data-testid="item-planning"], [data-testid="replan-status"]', { timeout: 10000 }).catch(() => {})
check('opening it starts 主 agent 規劃中… with no button to press', (await p.locator('[data-testid="replan-status"]').innerText().catch(() => '')).includes('主 agent') && (await p.locator('[data-testid="plan-replan"]').count()) === 0)
check('plan area bottom: only 核准並派發 (waits for the plan) and 討論', (await p.locator('[data-testid="plan-approve-wait"]').isDisabled()) && (await p.locator('[data-testid="plan-discuss"]').count()) === 1)
let selected = false
for (let i = 0; i < 20 && !selected; i++) {
  await post('/api/sim/step', { seconds: 30 })
  await p.waitForTimeout(400)
  selected = (await p.locator('[data-testid="plan-area"] [data-testid="approval-visual"]').count()) > 0
}
check('the resulting card is selected automatically with 核准並派發', selected && (await p.locator('[data-testid="plan-area"] [data-testid="visual-approve"]', { hasText: '核准並派發' }).count()) >= 1 && (await p.locator('[data-testid="plan-area"] [data-testid="main-plan-tag"]').count()) === 1)

// an event with an approval, so every page has something to show
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
const ranks = await p.locator('[data-testid="order-list"] .ot-row').evaluateAll((els) => els.map((e) => ['r-late', 'r-risk', 'r-ok', 'r-complete', 'r-done'].findIndex((c) => e.classList.contains(c))))
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

// (f) live state-machine diagram on the Agent page
check('Agent page: state diagram with the current state lit', await p.locator('[data-testid="state-diagram"] .sm-n.cur').count() === 1)
check('diagram draws the path taken', await p.locator('[data-testid="state-diagram"] .sm-e.on').count() >= 3)
check('diagram shows the round and the failure limit', (await p.locator('[data-testid="state-diagram"] .sm-stats').innerText()).includes('連續失敗'))
await p.locator('[data-testid="state-diagram"] .sm-n[data-state="simulate"]').click()
const filtered = await p.locator('[data-testid="trace-body"] > [data-kind]').count()
check('clicking a state filters the transcript', filtered >= 1 && (await p.locator('[data-testid="trace-body"]').innerText()).includes('只顯示在'), String(filtered))
await p.locator('[data-testid="state-diagram"] .sm-n[data-state="simulate"]').click()

// (h) 狀態機 page
await p.click('[data-testid="tab-sm"]')
await p.waitForSelector('[data-testid="sm-board"] [data-testid="sm-card"]', { timeout: 15000 }).catch(() => {})
check('狀態機: header counts', (await p.locator('[data-testid="sm-counts"] .kpi').count()) === 5)
check('狀態機: 11 state columns', await p.locator('[data-testid="sm-board"] .sm-col').count() === 11)
check('狀態機: cards for live runs (待核准 column)', await p.locator('.sm-col[data-state="await_approval"] [data-testid="sm-card"]').count() >= 1)
check('狀態機: 事件偵測 per line', await p.locator('[data-testid="sm-detect"] .sm-line').count() === 6)
check('狀態機: category chips with counts', await p.locator('[data-testid="sm-cats"] button').count() === 7)
await p.click('[data-testid="sm-cats"] button[data-cat="machine"]')
check('category filter keeps machine runs', await p.locator('[data-testid="sm-card"]').count() >= 1)
await p.click('[data-testid="sm-cats"] button[data-cat="ask"]')
await p.waitForSelector('[data-testid="sm-asks"]', { timeout: 5000 }).catch(() => {})
check('問戰情室 category lists asks', await p.locator('[data-testid="sm-asks"] li').count() >= 1)
// (g) 每日事件紀錄
await p.click('[data-testid="sm-view-day"]')
await p.waitForSelector('[data-testid="day-runs"] tbody tr', { timeout: 10000 })
check('每日事件紀錄: runs with state paths', await p.locator('[data-testid="day-runs"] .pp').count() >= 3)
check('每日事件紀錄: summary', (await p.locator('[data-testid="day-summary"]').innerText()).includes('平均結案時間'))
const csvHref = await p.locator('[data-testid="day-csv"]').getAttribute('href')
const csv = await p.evaluate(async (u) => { const r = await fetch(u); return { s: r.status, t: (await r.text()).slice(0, 60) } }, csvHref)
check('每日事件紀錄: CSV download', csv.s === 200 && csv.t.includes('id,類型'), csv.t.split('\n')[0])

// (d) 訂單 success filters
await p.click('[data-testid="tab-orders"]')
await p.waitForSelector('[data-testid="order-list"] .ot-row')
const segs = await p.locator('.order-tools .seg button').allInnerTexts()
check('訂單 filters 進行中 / 已完成 / 已出貨', ['進行中', '已完成', '已出貨'].every((x) => segs.some((s) => s.includes(x))), segs.join('|'))
await p.click('.order-tools .seg button:has-text("已出貨")')
check('shipped orders show a success line with on-time', (await p.locator('[data-testid="order-success"]').first().innerText()).includes('✓'))
await p.click('.order-tools .seg button:has-text("全部")')

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

// (e) 工廠指示 PLANT.md: save draft → 試跑 → 核准啟用 → rollback
await p.click('[data-testid="admin-plant"]')
await p.waitForSelector('[data-testid="plant-text"]')
const sha0 = await p.locator('[data-testid="plant-active-sha"]').innerText()
await p.locator('[data-testid="plant-text"]').fill((await p.locator('[data-testid="plant-text"]').inputValue()) + `\n- 測試用語：e2e ${Date.now()}。`)
await p.click('[data-testid="plant-save"]')
await p.waitForFunction(() => !document.querySelector('[data-testid="plant-test"]')?.hasAttribute('disabled'), null, { timeout: 5000 })
check('PLANT.md: activate is locked before a test run', await p.locator('[data-testid="plant-activate"]').isDisabled())
await p.click('[data-testid="plant-test"]')
await p.waitForSelector('[data-testid="plant-test-result"]', { timeout: 120000 })
check('PLANT.md: test run shows plan validity and the prompt diff', (await p.locator('[data-testid="plant-test-result"]').innerText()).includes('計劃有效'))
p.once('dialog', (d) => d.accept())
await p.click('[data-testid="plant-activate"]')
await p.waitForFunction((s0) => document.querySelector('[data-testid="plant-active-sha"]')?.textContent !== s0, sha0, { timeout: 5000 })
check('PLANT.md: activated (new sha)', (await p.locator('[data-testid="plant-active-sha"]').innerText()) !== sha0)
p.once('dialog', (d) => d.accept())
await p.locator('[data-testid="plant-history"] button', { hasText: '回復到這版' }).first().click()
await p.waitForFunction((s0) => document.querySelector('[data-testid="plant-active-sha"]')?.textContent === s0, sha0, { timeout: 5000 }).catch(() => {})
check('PLANT.md: rollback restores the previous version', (await p.locator('[data-testid="plant-active-sha"]').innerText()) === sha0)
const acts = await p.evaluate(() => fetch('/api/audit?kind=system&limit=5000').then((r) => r.json()).then((es) => es.filter((e) => e.actor.startsWith('admin:') && String(e.payload.admin).startsWith('plant_')).map((e) => e.payload.admin)))
check('PLANT.md: every step audited', ['plant_draft_save', 'plant_test', 'plant_activate', 'plant_rollback'].every((a) => acts.includes(a)), acts.join(','))

// reduced motion: the ticker does not scroll and wraps as a static row
{
  const c = await b.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' })
  const r = await c.newPage()
  await r.goto(BASE + '/', { waitUntil: 'networkidle' })
  await r.waitForSelector('[data-testid="tk-track"] .tk-i')
  const st = await r.evaluate(() => { const t = getComputedStyle(document.querySelector('[data-testid="tk-track"]')); return { a: t.animationName, w: t.flexWrap, dup: getComputedStyle(document.querySelector('.tk-dup')).display } })
  check('reduced motion: ticker static, wrapping, no duplicate', st.a === 'none' && st.w === 'wrap' && st.dup === 'none', JSON.stringify(st))
  await c.close()
}

// (d) success moments (order_completed / shipment_departed from the day loop): toast, ticker, 訂單 tab
await post('/api/sim/control', { action: 'scenario', scenario: 'day_loop' })
await p.goto(BASE + '/', { waitUntil: 'networkidle' })
await p.waitForTimeout(1200)
let toast = null
for (let i = 0; i < 48 && !toast; i++) {
  await post('/api/sim/step', { seconds: 1800 })
  await p.waitForTimeout(900)
  if (await p.locator('[data-testid="success-toast"]').count()) toast = await p.locator('[data-testid="success-toast"]').innerText()
}
check('主控台 shows a success toast for a new completion / shipment', !!toast && /訂單 .* 完成|已出貨/.test(toast), toast ?? '(none)')
check('ticker lists the success items', (await p.locator('[data-testid="tk-track"] > .tk-i.t-good').allInnerTexts()).some((t) => /完成|已出貨/.test(t)))
await p.click('[data-testid="tab-orders"]')
await p.waitForSelector('[data-testid="order-list"] .ot-row')
await p.click('.order-tools .seg button:has-text("已出貨")')
check('訂單 已出貨: time and on time / late', /出貨・(準時|逾期)/.test(await p.locator('[data-testid="order-success"]').first().innerText()))

// ticker contrast (WCAG AA 4.5:1) in both themes, every item and the fixed counts
for (const theme of ['light', 'dark']) {
  const c = await b.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' })
  await c.addInitScript((t) => { try { localStorage.setItem('warroom-theme', t) } catch { /* */ } }, theme)
  const r = await c.newPage()
  await r.goto(BASE + '/', { waitUntil: 'networkidle' })
  await r.waitForSelector('[data-testid="tk-track"] .tk-i')
  const worst = await r.evaluate(() => {
    const rgb = (s) => (s.match(/[\d.]+/g) || []).map(Number)
    const lum = ([r, g, b]) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4 }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b) }
    const bg = (el) => { for (let e = el; e; e = e.parentElement) { const c = rgb(getComputedStyle(e).backgroundColor); if (c.length >= 3 && (c[3] ?? 1) > 0.5) return c } return [255, 255, 255] }
    let w = { ratio: 99, text: '' }
    for (const el of document.querySelectorAll('.tk-i:not(.tk-dup .tk-i), .tk-fixed .as-item')) {
      const a = lum(rgb(getComputedStyle(el).color)), bb = lum(bg(el))
      const ratio = (Math.max(a, bb) + 0.05) / (Math.min(a, bb) + 0.05)
      if (ratio < w.ratio) w = { ratio: Math.round(ratio * 100) / 100, text: el.textContent.slice(0, 24) }
    }
    return w
  })
  check(`ticker contrast ≥ 4.5 (${theme})`, worst.ratio >= 4.5, `${worst.ratio} ${worst.text}`)
  await c.close()
}

// screenshots: every tab, 1440 and 390, light and dark
const TABS = [['agent', '/'], ['monitor', '/#monitor'], ['orders', '/#orders'], ['schedule', '/#schedule'], ['console', '/#console'], ['sm', '/#sm'], ['sm-day', '/#sm/day'], ['admin-rag', '/#admin/rag'], ['admin-plant', '/#admin/plant'], ['admin-memory', '/#admin/memory'], ['admin-status', '/#admin/status']]
for (const theme of ['light', 'dark']) {
  for (const [w, h] of [[1440, 900], [390, 844]]) {
    const c = await b.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: 1, timezoneId: 'Asia/Taipei', isMobile: w < 500, hasTouch: w < 500 })
    await c.addInitScript((t) => { try { localStorage.setItem('warroom-theme', t) } catch { /* */ } }, theme)
    const s = await c.newPage()
    s.on('pageerror', (e) => errors.push(`${w}/${theme}: ${e.message}`))
    for (const [name, path] of TABS) {
      if (w < 500 && !['agent', 'monitor', 'orders', 'console', 'sm', 'sm-day', 'admin-rag', 'admin-plant'].includes(name)) continue
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
