// Headless check of the 3D factory island (/factory) against a running FastAPI server (./start.sh, mock mode).
//   PORT=8010 ./start.sh &   then   node web/e2e/factory3d.mjs http://localhost:8010
// Checks: canvas renders, no console errors, third-person walking (WASD relative to the camera, Shift runs),
// Q/E and the wheel move the orbit camera, collision with the desk and the island edge, F at the desk starts
// the p.11 scenario, press 7 turns red, a new approval raises the HUD toast, P raises the real /phone page and
// it is usable (approve + 問戰情室). Screenshots -> web/e2e/screenshots/factory-*.png
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
const BASE = process.argv[2] ?? 'http://localhost:8000'
const OUT = resolve(dirname(fileURLToPath(import.meta.url)), 'screenshots')
mkdirSync(OUT, { recursive: true })

let failed = 0
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
async function until(fn, ms, step = 500) {
  const t0 = Date.now()
  while (Date.now() - t0 < ms) { const v = await fn(); if (v) return v; await sleep(step) }
  return null
}

// Headless (the default). SwiftShader gives WebGL without a GPU.
const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] })
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-TW', timezoneId: 'Asia/Taipei' })
const page = await ctx.newPage()
const errors = []
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
page.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`) })

try {
  // reset the sim so the run starts clean
  await page.request.post(`${BASE}/api/sim/control`, { data: { action: 'reset' } })
  await page.goto(`${BASE}/factory?test=1`, { waitUntil: 'networkidle' })
  const canvas = await page.waitForSelector('canvas', { timeout: 15000 })
  const box = await canvas.boundingBox()
  check('canvas renders full screen', !!box && box.width >= 1400 && box.height >= 850, JSON.stringify(box))
  const gl = await page.evaluate(() => { const c = document.querySelector('canvas'); return !!(c && (c.getContext('webgl2') || c.getContext('webgl'))) })
  check('WebGL context', gl)
  const live = await until(() => page.locator('.f3-conn.ok').count(), 10000)
  check('HUD connected to live backend', !!live, await page.locator('.f3-hud-top').innerText())

  await page.click('[data-testid=f3-start]')
  await sleep(3500)   // camera eases in from the island overview to behind the lead
  await page.screenshot({ path: `${OUT}/factory-start.png` })
  const pos = () => page.evaluate(() => ({ ...window.__factory3d.player }))
  const camNow = () => page.evaluate(() => ({ ...window.__factory3d.cam }))
  const hold = async (keys, ms) => { for (const k of keys) await page.keyboard.down(k); await sleep(ms); for (const k of [...keys].reverse()) await page.keyboard.up(k) }

  // Q/E orbit the camera around the figure; S walks toward the camera (away from the desk)
  const c0 = await camNow()
  await hold(['KeyQ'], 500)
  const c1 = await camNow()
  check('Q rotates the camera around the figure', Math.abs(c1.yaw - c0.yaw) > 0.05, `yaw ${c0.yaw.toFixed(2)} -> ${c1.yaw.toFixed(2)}`)
  await hold(['KeyE'], 500)
  const p0 = await pos()
  await hold(['KeyS'], 1500)
  const p1 = await pos()
  check('WASD walking moves the figure (S)', Math.hypot(p1.x - p0.x, p1.z - p0.z) > 0.3, `(${p0.x.toFixed(2)},${p0.z.toFixed(2)}) -> (${p1.x.toFixed(2)},${p1.z.toFixed(2)})`)
  check('the figure turns to face where it walks', Math.abs(Math.atan2(Math.sin(p1.yaw - p0.yaw), Math.cos(p1.yaw - p0.yaw))) > 1, `yaw ${p0.yaw.toFixed(2)} -> ${p1.yaw.toFixed(2)}`)
  // wheel zooms within limits
  await page.mouse.move(720, 450)
  await page.mouse.wheel(0, 600)
  await sleep(300)
  const c2 = await camNow()
  await page.mouse.wheel(0, -600)
  check('mouse wheel zooms the camera', c2.dist > c1.dist + 1 && c2.dist <= 24, `dist ${c1.dist.toFixed(1)} -> ${c2.dist.toFixed(1)}`)
  // Shift runs: same hold, roughly twice the distance
  await page.evaluate(() => window.__factory3d.teleport(0.5, 1.4, -Math.PI / 2))
  await hold(['KeyW'], 1000)
  const w1 = await pos()
  await page.evaluate(() => window.__factory3d.teleport(0.5, 1.4, -Math.PI / 2))
  await hold(['ShiftLeft', 'KeyW'], 1000)
  const r1 = await pos()
  check('Shift runs faster than walking', r1.x - 0.5 > (w1.x - 0.5) * 1.3, `walk ${(w1.x - 0.5).toFixed(2)} vs run ${(r1.x - 0.5).toFixed(2)}`)
  // collision: walk into the desk from behind it should stop short of it
  await page.evaluate(() => window.__factory3d.teleport(7.65, 0, Math.PI / 2))
  await hold(['KeyW'], 1500)
  const pc = await pos()
  check('collision: desk blocks the figure', pc.x > 7.38, `x=${pc.x.toFixed(2)} (desk edge 7.175 + radius 0.22)`)
  // the island edge: walking south into the sea stops on the grass
  await page.evaluate(() => window.__factory3d.teleport(0, 5.6, Math.PI))
  await hold(['ShiftLeft', 'KeyW'], 2500)
  const pe = await pos()
  const onIsle = await page.evaluate(([x, z]) => window.__factory3d.onIsland(x, z, 0.2), [pe.x, pe.z])
  check('collision: the island edge keeps the figure out of the sea', pe.z > 5.8 && onIsle, `z=${pe.z.toFixed(2)} onIsland=${onIsle}`)
  // zoomed-out view of the whole island
  await page.evaluate(() => { const f = window.__factory3d; f.teleport(0.5, 1.4, -0.6); f.cam.dist = 22 })
  await sleep(2500)
  await page.screenshot({ path: `${OUT}/factory-island.png` })
  await page.evaluate(() => { window.__factory3d.cam.dist = 14 })

  // desk: F opens the demo controls; start the p.11 scenario with the same API as DemoStrip
  await page.evaluate(() => window.__factory3d.teleport(6.2, 0, -Math.PI / 2))   // in front of the desk, facing the big screen
  await sleep(1200)
  check('desk prompt', (await page.locator('[data-testid=f3-prompt]').innerText()).includes('戰情室控制台'))
  await page.keyboard.press('KeyF')
  await page.waitForSelector('.f3-desk', { timeout: 3000 })
  await page.click('.f3-desk-main button.primary')
  await sleep(500)
  await page.click('.f3-desk .demo button:has-text("10×")')   // DemoStrip inside the desk panel: sim speed 10×
  await sleep(300)
  await page.click('.f3-desk-main button:has-text("前往 7 號硫化機")')
  const red = await until(async () => {
    const s = await (await page.request.get(`${BASE}/api/snapshot`)).json()
    const m = s.machines.find((x) => x.machine_id === 'CURE-07')
    return m && ['alarm', 'warning', 'stopped', 'out_of_service'].includes(m.status) ? m : null
  }, 90000, 1000)
  check('press 7 goes abnormal in the scenario', !!red, red ? `${red.status} ${red.temp_c}°C` : 'timeout')
  await sleep(1500)
  const tag = page.locator('[data-testid=tag-CURE-07]')
  const tagCls = (await tag.count()) ? await tag.getAttribute('class') : ''
  check('press 7 label shows red/amber + temperature', /red|amber/.test(tagCls ?? ''), `${tagCls} · ${(await tag.count()) ? (await tag.innerText()).replace(/\n/g, ' ') : ''}`)
  check('HUD shows the active event', (await page.locator('[data-testid=f3-event]').innerText()).includes('7 號硫化機'), await page.locator('[data-testid=f3-event]').innerText())
  await page.screenshot({ path: `${OUT}/factory-press7.png` })

  // machine panel: F near press 7
  await page.keyboard.press('KeyF')
  const drawer = await page.waitForSelector('.drawer', { timeout: 3000 }).catch(() => null)
  const tiles = await page.waitForSelector('.drawer .tiles .tile', { timeout: 8000 }).catch(() => null)
  check('F at a machine opens its live readings', !!drawer && !!tiles && (await drawer.innerText()).includes('7 號硫化機'), tiles ? (await page.locator('.drawer .tiles').innerText()).replace(/\n/g, ' ') : 'no tiles')
  await page.screenshot({ path: `${OUT}/factory-machine-panel.png` })
  await page.keyboard.press('Escape')
  await sleep(300)

  // approval arrives -> toast; P raises the real phone page
  const toast = await until(() => page.locator('[data-testid=f3-toast]').count(), 90000, 1000)
  check('new approval -> HUD toast', !!toast, toast ? await page.locator('[data-testid=f3-toast]').innerText() : 'no approval within 90 s')
  await page.keyboard.press('KeyP')
  await sleep(1200)
  const up = await page.locator('.f3-phone.up').count()
  check('P raises the phone', up === 1)
  const frame = page.frameLocator('.f3-phone iframe')
  await frame.locator('.phone').waitFor({ timeout: 10000 })
  await page.screenshot({ path: `${OUT}/factory-phone.png` })
  // usable: pick the first option and approve
  const opt = frame.locator('[role=radio]').first()
  if (await opt.count()) {
    const pendingN = async () => (await (await page.request.get(`${BASE}/api/snapshot`)).json()).pending_approvals.length
    const before = await pendingN()
    await opt.click()
    await frame.locator('button.ph-approve').first().click()
    const after = await until(async () => { const n = await pendingN(); return n < before ? String(n) : null }, 10000)
    check('approve inside the raised phone', !!after, `pending ${before} -> ${after}`)
  } else check('approval card in phone', false, 'no option radio found')
  // 問戰情室: type and send
  const ta = frame.locator('textarea[placeholder*="9 號機"]')
  if (await ta.count()) {
    await ta.fill('7 號硫化機現在溫度多少？')
    await frame.locator('button:has-text("送出問題")').click()
    const ans = await until(async () => { const a = frame.locator('[data-testid=ask-answer]'); return (await a.count()) ? (await a.last().innerText()).replace(/\n/g, ' ').slice(0, 120) : null }, 20000)
    check('問戰情室 answers inside the phone', !!ans, ans ?? 'no answer')
  } else check('問戰情室 box in phone', false)
  await page.screenshot({ path: `${OUT}/factory-phone-used.png` })
  await page.keyboard.press('Escape')
  await sleep(600)
  check('Esc lowers the phone', (await page.locator('.f3-phone.up').count()) === 0)

  // fps sample (headless SwiftShader: CPU rendering, far below a real GPU; reported, not asserted)
  const fps = await page.evaluate(() => new Promise((r) => { let n = 0; const t0 = performance.now(); const f = () => { n++; if (performance.now() - t0 < 2000) requestAnimationFrame(f); else r(n / 2) }; requestAnimationFrame(f) }))
  console.log(`INFO  fps headless SwiftShader: ${fps}`)

  // phone screen (390 px wide) — the HUD must still fit. Close the big page first: two SwiftShader pages starve each other.
  await page.close()
  const m = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: 'zh-TW', timezoneId: 'Asia/Taipei', hasTouch: true })
  const mp = await m.newPage()
  mp.on('pageerror', (e) => errors.push(`pageerror(390): ${e.message}`))
  await mp.goto(`${BASE}/factory?test=1`, { waitUntil: 'domcontentloaded' })   // the 10× sim keeps the network busy
  await mp.waitForSelector('canvas', { timeout: 15000 })
  await sleep(1500)
  await mp.click('[data-testid=f3-start]')
  await sleep(3500)
  await mp.screenshot({ path: `${OUT}/factory-390.png` })
  const over = await mp.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)
  check('390 px: no horizontal overflow', !over)
  await m.close()
} finally {
  const real = errors.filter((e) => !/favicon/.test(e))
  check('no console errors', real.length === 0, real.slice(0, 5).join(' | '))
  await browser.close()
}
console.log(failed ? `${failed} FAILED` : 'ALL PASS')
process.exit(failed ? 1 : 0)
