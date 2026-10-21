// End-to-end smoke + screenshots against `pnpm preview` (mock mode, no backend needed).
//   pnpm build && pnpm exec vite preview --port 4173 &   then   node e2e/shots.mjs [baseUrl]
// Uses the `playwright` package if installed, else the playwright-core bundled with @playwright/cli.
import { createRequire } from 'node:module'
import { mkdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
function loadPlaywright() {
  for (const id of ['playwright', 'playwright-core', `${homedir()}/.local/lib/node_modules/@playwright/cli/node_modules/playwright-core`]) {
    try { return require(id) } catch { /* next */ }
  }
  throw new Error('playwright not found: pnpm add -D playwright, or install @playwright/cli')
}
const { chromium } = loadPlaywright()
const BASE = process.argv[2] ?? 'http://localhost:4173'
const OUT = resolve(dirname(fileURLToPath(import.meta.url)), '../../docs/screenshots')
mkdirSync(OUT, { recursive: true })

const results = []
const check = (name, ok, detail = '') => { results.push({ name, ok, detail }); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }

const browser = await chromium.launch()
const errors = []
async function page(w, h, url) {
  const ctx = await browser.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: 2, locale: 'zh-TW', timezoneId: 'Asia/Taipei' })
  const p = await ctx.newPage()
  p.on('pageerror', (e) => errors.push(`${url}: ${e.message}`))
  p.on('console', (m) => { if (m.type() === 'error') errors.push(`${url}: ${m.text()}`) })
  await p.goto(BASE + url, { waitUntil: 'networkidle' })
  return p
}
// plain.ts puts word joiners (U+2060) inside 判斷一 so it never breaks across lines
const text = async (p) => (await p.locator('body').innerText()).replace(/\u2060/g, '')

// 1. page 2 — open-mill overheat at 14:26, desktop 1440
{
  const p = await page(1440, 900, '/?mock=1&scenario=open_mill_overheat&at=14:26:00&speed=1')
  await p.waitForSelector('.focus-title')
  const t = await text(p)
  check('p2 title', t.includes('實心胎智慧製造戰情室'))
  check('p2 chips 27/1', t.includes('正常 27 台') && t.includes('異常 1 台'), t.match(/正常 \d+ 台|異常 \d+ 台/g)?.join(' '))
  check('p2 orders', t.includes('38') && t.includes('剩餘約 208 小時') && t.includes('剩餘約 1,241 小時') && t.includes('剩餘約 2,162 小時'))
  check('p2 semi', ['預熱中', '成形中', '包布中', '已完成'].every((s) => t.includes(s)))
  check('p2 focus', t.includes('異常：A 線大開煉機') && t.includes('溫度過高') && t.includes('冷卻能力不足'))
  check('p2 tiles', t.includes('112°C') && t.includes('偏低') && t.includes('14 分'), t.match(/\d+°C/)?.[0])
  check('p2 actions', t.includes('已自動：通知維修冷卻水路') && t.includes('待核准：放慢生料投入'))
  check('p2 impact', t.includes('排程影響：無，大胎 #07 仍 17:00 入硫化機'))
  check('p2 alarm dot', (await p.locator('.mdot.st-alarm[data-machine="A-BIGOPEN"]').count()) === 1)
  await p.screenshot({ path: `${OUT}/dashboard-p2-1440.png`, fullPage: true })
  // drawer
  await p.click('[data-machine="A-BIGOPEN"]')
  await p.waitForSelector('.drawer .chart')
  const dt = await p.locator('.drawer').innerText()
  check('drawer tiles', ['溫度', '壓力', '時間', '水流'].every((s) => dt.includes(s)) && dt.includes('未加硫'))
  await p.screenshot({ path: `${OUT}/machine-drawer-1440.png` })
  await p.keyboard.press('Escape')
  // approve
  await p.click('.btn-approve')
  await p.waitForTimeout(800)
  const t2 = await text(p)
  check('p2 approve → no pending', !t2.includes('待核准：放慢生料投入'))
  await p.context().close()
}

// 2. page 11 — press 7 at 02:31:08 (round 2 pending), dashboard + schedule tab
{
  const p = await page(1440, 900, '/?mock=1&scenario=press7_temp_drop&at=02:31:15&speed=1')
  await p.waitForSelector('.focus-title')
  const t = await text(p)
  check('p11 focus', t.includes('7 號硫化機') && t.includes('判斷一不成立'))
  check('p11 pending A/B', t.includes('A 夜班加班') && t.includes('B 從 2 線調 1 人'))
  await p.screenshot({ path: `${OUT}/dashboard-p11-1440.png`, fullPage: true })
  // UX declutter: the event timeline left 人力排程 (was ⑦); the whole of it is read in 產線監控 ③ via 看全部
  await p.click('.tabs button:has-text("產線監控")')
  await p.click('[data-testid=focus-timeline] > summary')
  await p.click('[data-testid=timeline-all]').catch(() => {})
  const tl = await (p.locator('[data-testid=focus-timeline]').innerText())
  check('timeline rows', ['02:14:00', '02:14:01', '02:14:11', '02:16:40', '02:31:00', '02:31:04'].every((x) => tl.includes(x)))
  check('timeline numbers', tl.includes('28.5 h') && tl.includes('+4.5 h') && tl.includes('119.5°C') && tl.includes('#A-311'))
  await p.click('text=人力排程')
  await p.waitForSelector('.gantt svg')
  await p.evaluate(() => { const d = document.querySelector('details.plan-panel'); if (d) d.open = true })   // ⑥ is folded by default
  await p.waitForTimeout(400)
  const s = await text(p)
  check('schedule versions', s.includes('第 1 版') && s.includes('第 2 版') && s.includes('第 3 版'))
  check('schedule steps', s.includes('solve_schedule') && s.includes('sop_search') && s.includes('換假設'))
  check('labor chart', s.includes('硫化機（運轉）') && s.includes('胎體／胎面膠組'))
  check('capacity', s.includes('13.3 → 9.3 週') && s.includes('4.9'))
  check('gantt A-311 on CURE-09', await p.evaluate(() => [...document.querySelectorAll('.g-job.hot title')].some((t) => t.textContent?.includes('9 號硫化機'))))
  await p.screenshot({ path: `${OUT}/schedule-p11-1440.png`, fullPage: true })
  // rollback
  await p.click('text=回復上一版')
  await p.waitForTimeout(500)
  check('rollback', (await text(p)).includes('已回復到第'))
  await p.context().close()
}

// 3. phone 390 — page-11 round-2 card
{
  const p = await page(390, 844, '/phone?mock=1&scenario=press7_temp_drop&at=02:31:15&speed=1')
  await p.waitForSelector('.ph-card')
  const t = await text(p)
  check('phone card', t.includes('第二輪核准') && t.includes('7 號機停用，重排中') && t.includes('119.5°C') && t.includes('#A-311 改 9 號') && t.includes('夜班缺 1 名操作員') && t.includes('不受影響'))
  check('phone options', t.includes('夜班加班') && t.includes('從 2 線調 1 人'))
  await p.click('.ph-opt >> nth=0')
  await p.screenshot({ path: `${OUT}/phone-p11-390.png`, fullPage: true })
  await p.click('.ph-approve')
  await p.waitForTimeout(800)
  const t2 = await text(p)
  check('phone approve → resolved', t2.includes('目前沒有待核准事項') && t2.includes('已核准'), t2.slice(0, 200).replace(/\n/g, ' | '))
  await p.screenshot({ path: `${OUT}/phone-after-390.png`, fullPage: true })
  await p.context().close()
}

// 4. dashboard at phone width
{
  const p = await page(390, 844, '/?mock=1&scenario=open_mill_overheat&at=14:26:00&speed=1')
  await p.waitForSelector('.focus-title')
  const overflow = await p.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
  check('390 no horizontal scroll', overflow <= 1, `overflow ${overflow}px`)
  await p.screenshot({ path: `${OUT}/dashboard-390.png`, fullPage: true })
  await p.context().close()
}

// 5. offline tier + phone/dashboard sync over BroadcastChannel (default auto mode, same context)
{
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-TW', timezoneId: 'Asia/Taipei' })
  const d = await ctx.newPage()
  d.on('pageerror', (e) => errors.push(`sync dash: ${e.message}`))
  await d.goto(BASE + '/?mock=1#monitor', { waitUntil: 'networkidle' })
  await d.waitForSelector('.focus-title')
  const ph = await ctx.newPage()
  await ph.goto(BASE + '/phone?mock=1', { waitUntil: 'networkidle' })
  await ph.waitForSelector('.ph-card')
  check('sync: phone sees dashboard approval', (await ph.locator('body').innerText()).includes('放慢生料投入'))
  await ph.click('.ph-approve')
  await d.waitForTimeout(800)
  check('sync: dashboard pending cleared by phone', !(await d.locator('body').innerText()).includes('待核准：放慢生料投入'))
  await d.click('text=LAN 斷')
  await d.waitForTimeout(400)
  check('lan_down banner', (await d.locator('body').innerText()).includes('LAN 中斷'))
  await d.screenshot({ path: `${OUT}/dashboard-lan-down-1440.png` })
  await ctx.close()
}

await browser.close()
check('no page errors', errors.length === 0, errors.slice(0, 5).join(' || '))
const failed = results.filter((r) => !r.ok)
console.log(`\n${results.length - failed.length}/${results.length} checks passed; screenshots in ${OUT}`)
process.exit(failed.length ? 1 : 0)
