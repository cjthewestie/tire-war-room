// Headless e2e for the 待核准 bar on every page (CJ, 2026-10-22: 「核准區有代辦時應該全部頁面都要看得到告警」), against a
// running server: loads 3 件同時異常 (concurrent_3), waits for pending cards, pauses, then checks every war-room tab,
// /report, /handover and /factory show the bar, /phone and /worker do not, the tab title carries 「(N) 待核准・」, and a
// click lands on 主控台 with the oldest card selected. Screenshots go to the OUT dir.
//   node e2e/approval-alert.mjs http://localhost:8220 [outDir]
import { createRequire } from 'node:module'
import { mkdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { resolve } from 'node:path'

const require = createRequire(import.meta.url)
let pw
for (const id of ['playwright', 'playwright-core', `${homedir()}/.local/lib/node_modules/@playwright/cli/node_modules/playwright-core`]) { try { pw = require(id); break } catch { /* next */ } }
const BASE = process.argv[2] ?? 'http://localhost:8220'
const OUT = resolve(process.argv[3] ?? 'e2e/screenshots/approval-alert')
mkdirSync(OUT, { recursive: true })
let failed = 0
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }
const sim = (page, body) => page.evaluate((b) => fetch('/api/sim/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) }).then((x) => x.status), body)
const pendingNow = (page) => page.evaluate(() => fetch('/api/approvals?status=pending').then((x) => x.json()))

const b = await pw.chromium.launch({ headless: true, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] })
const ctxOf = (width, height) => b.newContext({ viewport: { width, height }, deviceScaleFactor: 1, timezoneId: 'Asia/Taipei', locale: 'zh-TW' })
try {
  const ctx = await ctxOf(1440, 900)
  const d = await ctx.newPage()
  await d.goto(`${BASE}/?live=1`, { waitUntil: 'domcontentloaded' })
  await d.waitForSelector('[data-testid=link-chip].live', { timeout: 30000 })

  const r = [await sim(d, { action: 'scenario', scenario: 'concurrent_3' }), await sim(d, { action: 'speed', speed: 30 }), await sim(d, { action: 'play' })]
  check('concurrent_3 started', r.every((x) => x === 200), r.join(','))
  let pending = []
  for (let i = 0; i < 240 && pending.length < 3; i++) { await d.waitForTimeout(1000); pending = await pendingNow(d) }
  await sim(d, { action: 'pause' })
  await d.waitForTimeout(1500)
  pending = await pendingNow(d)
  check('cards pending', pending.length > 0, String(pending.length))
  const oldest = [...pending].sort((a, c) => (a.created_at < c.created_at ? -1 : 1))[0]

  const bar = d.locator('[data-testid=pending-alert]')
  for (const [tab, name] of [['agent', '主控台'], ['monitor', '產線監控'], ['schedule', '人力排程'], ['orders', '訂單'], ['console', 'Agent'], ['sm', '狀態機'], ['admin', '後台']]) {
    await d.locator(`.topbar .tabs button:has-text("${name}")`).first().click()
    await d.waitForTimeout(700)
    const shown = await bar.isVisible()
    const txt = shown ? await bar.innerText() : ''
    check(`${name}: bar shown`, shown && txt.includes(`待核准 ${pending.length} 件`), txt.replace(/\s+/g, ' ').slice(0, 90))
    check(`${name}: points at the oldest card`, (await bar.getAttribute('data-event')) === oldest.event_id)
    if (['monitor', 'sm', 'admin'].includes(tab)) await d.screenshot({ path: `${OUT}/${tab}-1440.png` })
    if (tab === 'agent') check('主控台: compact', await d.locator('.paa-wrap-compact').isVisible())
  }
  check('title prefix', (await d.title()).startsWith(`(${pending.length}) 待核准・`), await d.title())
  // sticky: still on screen after a long scroll
  await d.locator('.topbar .tabs button:has-text("產線監控")').first().click()
  await d.mouse.wheel(0, 2400); await d.waitForTimeout(400)
  const box = await bar.boundingBox()
  check('sticky under the header after scrolling', !!box && box.y >= 0 && box.y < 140, JSON.stringify(box))
  // click → 主控台, oldest card selected
  await bar.locator('.paa').click()
  await d.waitForTimeout(800)
  check('click → 主控台 tab', await d.locator('[data-testid=tab-agent][aria-selected=true]').count() === 1)
  const on = d.locator(`[data-testid="home-item-${oldest.event_id}"]`)
  check('… the oldest card selected', (await on.getAttribute('aria-pressed')) === 'true', oldest.event_id)

  for (const path of ['/report', '/handover']) {
    const p = await ctx.newPage()
    await p.goto(`${BASE}${path}?live=1`, { waitUntil: 'domcontentloaded' })
    const pb = p.locator('[data-testid=pending-alert]')
    await pb.waitFor({ timeout: 15000 }).catch(() => {})
    check(`${path}: bar shown`, await pb.isVisible())
    check(`${path}: link to 主控台 with the oldest card`, (await pb.locator('a').getAttribute('href'))?.endsWith(`#approve?event=${encodeURIComponent(oldest.event_id)}`))
    check(`${path}: title prefix`, (await p.title()).startsWith(`(${pending.length}) 待核准・`), await p.title())
    if (path === '/report') await p.screenshot({ path: `${OUT}/report-1440.png` })
    await p.close()
  }
  {
    const p = await ctx.newPage()
    await p.goto(`${BASE}/factory?live=1`, { waitUntil: 'domcontentloaded' })
    const pb = p.locator('[data-testid=pending-alert]')
    await pb.waitFor({ timeout: 30000 }).catch(() => {})
    await p.waitForTimeout(4000)
    check('/factory: corner overlay', await p.locator('.paa-wrap-corner').isVisible())
    const fb = await pb.boundingBox()
    check('/factory: small, in the top-right corner', !!fb && fb.x > 1440 / 2 && fb.y < 40 && fb.width <= 380, JSON.stringify(fb))
    await p.screenshot({ path: `${OUT}/factory-1440.png` })
    await p.close()
  }
  for (const path of ['/phone', '/worker']) {
    const p = await ctx.newPage()
    await p.goto(`${BASE}${path}?live=1`, { waitUntil: 'domcontentloaded' })
    await p.waitForTimeout(2500)
    check(`${path}: no bar (own approval UI / workers do not approve)`, await p.locator('[data-testid=pending-alert]').count() === 0)
    await p.close()
  }
  // phone width
  {
    const c = await ctxOf(390, 844)
    const p = await c.newPage()
    await p.goto(`${BASE}/?live=1#monitor`, { waitUntil: 'domcontentloaded' })
    const pb = p.locator('[data-testid=pending-alert]')
    await pb.waitFor({ timeout: 15000 }).catch(() => {})
    await p.waitForTimeout(1200)
    const bx = await pb.boundingBox()
    check('390: bar fits the width', !!bx && bx.width <= 390 && (await p.evaluate(() => document.documentElement.scrollWidth)) <= 390, JSON.stringify(bx))
    await p.screenshot({ path: `${OUT}/monitor-390.png` })
    await c.close()
  }
  await sim(d, { action: 'play' })
} finally {
  await b.close()
}
console.log(failed ? `\n${failed} FAILED` : '\nall passed')
process.exit(failed ? 1 : 0)
