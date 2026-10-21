// Headless check of the whole-plant world (issue #11) against a running FastAPI server.
//   uv run python -m warroom.api serve --port 8070 &   then   node web/e2e/world.mjs http://127.0.0.1:8070
// Checks: GET /api/world has every domain; the dashboard 工廠全貌 tab renders them and updates live (WS "world");
// it fits 390 px without sideways scroll; in the 3D factory the on-duty workers are figures that walk to their
// zones, and the dock truck drives in for a shipment / a delivery and away when it leaves.
// Screenshots -> web/e2e/screenshots/world-*.png
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
const BASE = process.argv[2] ?? 'http://127.0.0.1:8070'
const OUT = resolve(dirname(fileURLToPath(import.meta.url)), 'screenshots')
mkdirSync(OUT, { recursive: true })

let failed = 0
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
async function until(fn, ms, step = 400) {
  const t0 = Date.now()
  while (Date.now() - t0 < ms) { const v = await fn(); if (v) return v; await sleep(step) }
  return null
}

// Headless (the default). SwiftShader gives WebGL without a GPU.
const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] })
const errors = []
const watch = (page) => {
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
  page.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`) })
}
const api = (path, data) => fetch(`${BASE}${path}`, data ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) } : undefined).then((r) => r.json())
/** Step the sim to factory-local HH:MM of the current day. */
async function stepTo(hh, mm) {
  const h = await api('/api/health')
  const now = new Date(h.factory_time)
  const target = new Date(`${h.factory_time.slice(0, 10)}T${String(hh).padStart(2, '0')}:${String(mm).padStart(2, '0')}:00+08:00`)
  let s = Math.round((target - now) / 1000)
  while (s > 0) { const k = Math.min(s, 6 * 3600); await api('/api/sim/step', { seconds: k }); s -= k }
}

try {
  await api('/api/sim/control', { action: 'scenario', scenario: 'normal' })
  await api('/api/sim/control', { action: 'speed', speed: 30 })
  const w0 = await api('/api/world')
  const domains = ['orders', 'materials', 'purchase_orders', 'finished_goods', 'dock', 'people', 'energy', 'quality', 'maintenance', 'events']
  check('GET /api/world has every domain', domains.every((k) => k in w0), Object.keys(w0).join(','))
  check('people: named workers with state and zone', w0.people.total >= 20 && w0.people.workers.every((x) => x.name && x.state && x.zone), `${w0.people.on_duty}/${w0.people.total} on duty`)

  // ---------------------------------------------------------------- dashboard 1440
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-TW', timezoneId: 'Asia/Taipei' })
  const page = await ctx.newPage()
  watch(page)
  await page.goto(`${BASE}/?live=1#world`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[data-testid=plant-panels]', { timeout: 15000 })
  // UX declutter: the pipeline sits in ①'s folded 訂單明細 and energy / dock / goods in 更多全廠資訊 — read text, then open them
  const pipe = await page.locator('[data-testid=order-pipeline] li').allTextContents()
  await page.evaluate(() => document.querySelectorAll('[data-testid=orders-more], [data-testid=plant-more]').forEach((d) => { d.open = true }))
  check('order pipeline shows the five stages', pipe.length === 5 && ['待排程', '生產中', '完成', '待出貨', '已出貨'].every((s, i) => pipe[i].includes(s)), pipe.map((t) => t.replace(/\n/g, '')).join(' | '))
  check('material bars with reorder lines', (await page.locator('[data-testid=materials] .mat-bar em').count()) === 6)
  check('people by zone', (await page.locator('[data-testid=people-zones] li').count()) >= 4, (await page.locator('[data-testid=people-zones]').innerText()).replace(/\n/g, ' '))
  check('energy panel in kW', /\d+ ?kW/.test(await page.locator('.w-energy').innerText()))
  const rev0 = Number(await page.locator('[data-testid=plant-panels]').getAttribute('data-rev'))
  const moved = await until(async () => Number(await page.locator('[data-testid=plant-panels]').getAttribute('data-rev')) > rev0, 15000)
  check('world updates live over the WebSocket', !!moved, `rev ${rev0} -> ${await page.locator('[data-testid=plant-panels]').getAttribute('data-rev')}`)
  await page.screenshot({ path: `${OUT}/world-1440.png`, fullPage: true })

  // ---------------------------------------------------------------- 3D factory: workers + truck
  const f3 = await ctx.newPage()
  watch(f3)
  await f3.goto(`${BASE}/factory?test=1`, { waitUntil: 'domcontentloaded' })
  await f3.waitForSelector('.f3-canvas canvas', { timeout: 15000 })
  // CJ 2026-10-20 「恢復人力待命 有一兩個人巡邏」: people stand by in the staff corner, two patrol; nobody else walks
  // unless they have a task (issue #17; scenarios.mjs checks a task walker)
  const ready = await until(() => f3.evaluate(() => !!window.__world3d && !!window.__patrol3d), 20000)
  check('3D: live world hooked up', !!ready)
  const idle = await f3.evaluate(() => ({ task: window.__world3d.walkers().length, patrol: window.__patrol3d() }))
  check('3D: with no task only the patrol walks', idle.task === 0 && idle.patrol >= 1 && idle.patrol <= 2, `task walkers ${idle.task}, patrol ${idle.patrol}`)
  await f3.evaluate(() => { document.querySelector('[data-testid=f3-start]').style.visibility = 'hidden' })
  await f3.screenshot({ path: `${OUT}/world-factory-people.png` })

  // a shipment truck comes 45 min before the 10:00 dispatch when an order waits to ship
  await f3.bringToFront()
  await stepTo(9, 20)
  const wShip = await api('/api/world')
  check('dock: outbound truck at 09:20', wShip.dock.trucks.some((t) => t.kind === 'outbound'), wShip.dock.trucks.map((t) => t.cargo).join(' / '))
  const parked = await until(() => f3.evaluate(() => { const t = window.__truck3d(); return t.visible && Math.abs(t.z - t.parkedZ) < 0.05 && t.cargo === 'outbound' ? t : null }), 25000)
  check('3D: truck drives in and parks with tyres', !!parked, JSON.stringify(parked))
  const still = await f3.evaluate(() => window.__world3d.walkers().length)
  check('3D: the world moving (truck, shifts) sends nobody walking without a task', still === 0, `${still} task walkers`)
  await f3.screenshot({ path: `${OUT}/world-factory-truck.png` })
  await page.screenshot({ path: `${OUT}/world-1440-dock.png`, fullPage: true })
  check('dashboard: truck card while it loads', (await page.locator('[data-testid=dock-trucks] .truck').count()) >= 1)

  // 10:00 the shipment leaves; the nylon delivery due 10:00 arrives, unloads 30 min, leaves
  await stepTo(10, 5)
  const wIn = await api('/api/world')
  check('dock: shipment left, delivery unloading', wIn.dock.trucks.some((t) => t.kind === 'inbound') && !wIn.dock.trucks.some((t) => t.kind === 'outbound') &&
    wIn.orders.items.some((o) => o.status === '已出貨' && o.shipped_at), wIn.dock.trucks.map((t) => t.cargo).join(' / '))
  const inb = await until(() => f3.evaluate(() => { const t = window.__truck3d(); return t.visible && t.cargo === 'inbound' ? t : null }), 15000)
  check('3D: delivery truck with crates', !!inb)
  await stepTo(10, 45)
  const gone = await until(() => f3.evaluate(() => (window.__truck3d().visible ? null : true)), 25000)
  const wOut = await api('/api/world')
  check('3D: truck drives off when the dock is empty', !!gone && wOut.dock.trucks.length === 0)
  check('delivery received into stock', wOut.purchase_orders.some((p) => p.id === 'PO-1001' && p.status === '已收貨'))

  // ---------------------------------------------------------------- 390 px
  const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, locale: 'zh-TW', timezoneId: 'Asia/Taipei', isMobile: true, hasTouch: true })
  const p3 = await phone.newPage()
  watch(p3)
  await p3.goto(`${BASE}/?live=1#world`, { waitUntil: 'domcontentloaded' })
  await p3.waitForSelector('[data-testid=plant-panels]', { timeout: 15000 })
  await sleep(800)
  const sw = await p3.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }))
  check('390 px: no sideways scroll', sw.sw <= sw.cw + 1, JSON.stringify(sw))
  check('390 px: 產線監控 (with the whole plant) in the bottom nav', (await p3.locator('.bottomnav button', { hasText: "產線監控" }).count()) === 1)
  const wraps = await p3.evaluate(() => [...document.querySelectorAll('.bottomnav button')].map((b) => {
    const t = [...b.childNodes].filter((n) => n.nodeType === 3 && n.textContent.trim())
    const r = document.createRange(); r.selectNodeContents(t[0] ?? b)
    return { label: (t[0]?.textContent ?? '').trim(), lines: new Set([...r.getClientRects()].map((x) => Math.round(x.top))).size }
  }))
  check('390 px: no bottom-nav label wraps', wraps.length === 7 && // 7 since #52: 狀態機 and 後台 moved under 更多 on phones
    wraps.every((w) => w.lines === 1), wraps.map((w) => `${w.label}:${w.lines}`).join(' '))
  await p3.screenshot({ path: `${OUT}/world-390.png`, fullPage: true, scale: 'css' })   // the page grew (tasks + goods panels): CSS scale keeps it under the capture limit
  await p3.screenshot({ path: `${OUT}/world-390-viewport.png` })

  // team dataset: page 11 plays the dataset's EV-2 night, where 貼合組 S11 is on sick leave
  check('sources footnote names the team dataset', (await p3.locator('[data-testid=world-sources]').innerText()).includes('團隊資料集'))
  await api('/api/sim/control', { action: 'scenario', scenario: 'press7_temp_drop' })
  const w11 = await api('/api/world')
  const s11 = w11.people.workers.find((x) => x.id === 'S11')
  check('p.11: S11 on sick leave from the team roster', s11?.state === '請假' && s11?.source === 'team_dataset', `${s11?.state} ${s11?.note}`)
  await page.bringToFront()
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[data-testid=plant-panels]', { timeout: 15000 })
  const ppl = await until(async () => { const t = await page.locator('.w-people').innerText(); return t.includes('S11') ? t : null }, 10000)
  check('dashboard shows S11 病假 on the p.11 night', !!ppl && ppl.includes('病假'), (ppl ?? '').split('\n').filter((l) => l.includes('S11')).join(' '))
  check('incident history from the dataset', (await page.locator('[data-testid=incidents] li').count()) === 3)
  await page.screenshot({ path: `${OUT}/world-1440-p11.png`, fullPage: true })
  await api('/api/sim/control', { action: 'scenario', scenario: 'normal' })

  check('no console errors', errors.length === 0, errors.slice(0, 3).join(' | '))
} catch (e) {
  failed++
  console.log('FAIL  exception', e)
} finally {
  await browser.close()
}
console.log(failed ? `\n${failed} FAILED` : '\nALL PASS')
process.exit(failed ? 1 : 0)
