// Headless check of the 3D factory (/factory, a dollhouse-cutaway factory building) against a running FastAPI
// server (./start.sh, mock mode).
//   PORT=8010 ./start.sh &   then   node web/e2e/factory3d.mjs http://localhost:8010
// Checks: canvas renders, no console errors, the whole building fits the start overview, third-person walking (WASD
// relative to the camera, Shift runs), Q/E and the wheel move the orbit camera, collision with the desk, the inner
// glass walls and the outer walls, doorways stay open (war room, the doors out to the loading dock), line and room
// signs show, F at the desk starts the p.11 scenario, press 7 turns red, a new approval raises the HUD toast, P raises the real /phone page and
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

  // before start: the camera circles the whole building; all four corners and the dock are on screen
  await sleep(2500)
  const corners = () => page.evaluate(() => {
    const f = window.__factory3d, B = { x0: -8.0, x1: 11.6, z0: -6.0, z1: 6.2 }
    const pts = [[B.x0, B.z0], [B.x1, B.z0], [B.x0, B.z1], [B.x1, B.z1], [7.1, 8.0]].map(([x, z]) => f.screenOf(x, 0, z))
    return { dist: f.cam.dist, ok: pts.every((s) => s && s.x > 0 && s.x < innerWidth && s.y > 0 && s.y < innerHeight), pts: pts.map((s) => `${s.x.toFixed(0)},${s.y.toFixed(0)}`).join(' ') }
  })
  const pre = await corners()
  check('start overview shows the whole building', pre.ok, `corners+dock ${pre.pts}`)
  await page.evaluate(() => { document.querySelector('[data-testid=f3-start]').style.visibility = 'hidden' })
  await page.screenshot({ path: `${OUT}/factory-overview.png` })
  await page.evaluate(() => { document.querySelector('[data-testid=f3-start]').style.visibility = '' })
  const rooms = await page.evaluate(() => [...document.querySelectorAll('[data-testid^=room-]')].map((e) => e.textContent))
  check('room signs: 戰情室, 員工待命區, 休息區, 廁所', ['戰情室', '員工待命區', '休息區', '廁所'].every((r) => rooms.includes(r)), rooms.join(' · '))
  const signs = await page.evaluate(() => [...document.querySelectorAll('[data-testid^=sign-]')].map((e) => e.textContent))
  check('all six line signs show (DOM, unblurred)', signs.length === 6, signs.join(' · '))

  await page.click('[data-testid=f3-start]')
  await sleep(3500)   // camera eases in from the building overview to behind the lead
  await page.screenshot({ path: `${OUT}/factory-start.png` })
  const pos = () => page.evaluate(() => ({ ...window.__factory3d.player }))
  const camNow = () => page.evaluate(() => ({ ...window.__factory3d.cam }))
  // the start view looks across the works: most machines are on screen, not behind the camera
  const onScreen = await page.evaluate(() => {
    const f = window.__factory3d
    return Object.values(f.placed).filter((p) => { const s = f.screenOf(p.x, p.h / 2, p.z); return s && s.x > 0 && s.x < innerWidth && s.y > 0 && s.y < innerHeight }).length
  })
  check('start view faces the machine rows', onScreen >= 14, `${onScreen}/28 machines on screen`)
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
  check('mouse wheel zooms the camera', c2.dist > c1.dist + 1 && c2.dist <= 36, `dist ${c1.dist.toFixed(1)} -> ${c2.dist.toFixed(1)}`)
  // wheel all the way out from the middle of the building: the whole building fits on screen
  await page.evaluate(() => window.__factory3d.teleport(1.8, 0.8))
  for (let i = 0; i < 6; i++) await page.mouse.wheel(0, 800)
  await sleep(2500)
  const fit = await corners()
  check('wheel max shows the whole building', fit.ok, `dist ${fit.dist.toFixed(1)} · corners+dock ${fit.pts}`)
  await page.screenshot({ path: `${OUT}/factory-wheel-max.png` })
  await page.evaluate(() => { window.__factory3d.cam.dist = 20 })
  // Shift runs: same hold, roughly twice the distance
  await page.evaluate(() => window.__factory3d.teleport(0.5, 1.4, -Math.PI / 2))
  await hold(['KeyW'], 1000)
  const w1 = await pos()
  await page.evaluate(() => window.__factory3d.teleport(0.5, 1.4, -Math.PI / 2))
  await hold(['ShiftLeft', 'KeyW'], 1000)
  const r1 = await pos()
  check('Shift runs faster than walking', r1.x - 0.5 > (w1.x - 0.5) * 1.3, `walk ${(w1.x - 0.5).toFixed(2)} vs run ${(r1.x - 0.5).toFixed(2)}`)
  // collision: walk into the desk from behind it (between the desk and the big screen) should stop short of it
  await page.evaluate(() => window.__factory3d.teleport(9.1, 0, Math.PI / 2))
  await hold(['KeyW'], 1500)
  const pc = await pos()
  check('collision: desk blocks the figure', pc.x > 8.76, `x=${pc.x.toFixed(2)} (desk edge 8.575 + radius 0.22)`)
  // the war room's glass wall blocks; its doorway (toward the C/D aisle) lets the lead through
  await page.evaluate(() => window.__factory3d.teleport(6.8, -1.5, 0))
  await hold(['KeyW'], 1500)
  const pg = await pos()
  check('collision: war-room glass wall blocks', pg.z > -2.13 && pg.z < -1.6, `z=${pg.z.toFixed(2)} (glass at -2.4 + 0.08 + 0.22)`)
  await page.evaluate(() => window.__factory3d.teleport(4.0, 0, -Math.PI / 2))
  await hold(['ShiftLeft', 'KeyW'], 1500)
  const pd = await pos()
  check('war-room doorway is open', pd.x > 5.2, `x=${pd.x.toFixed(2)}`)
  // the outer wall: running south off the production floor stops at the wall
  await page.evaluate(() => window.__factory3d.teleport(0, 5.6, Math.PI))
  await hold(['ShiftLeft', 'KeyW'], 2500)
  const pe = await pos()
  const inWalls = await page.evaluate(([x, z]) => window.__factory3d.walkable(x, z, 0.2), [pe.x, pe.z])
  check('collision: the outer wall keeps the figure inside', pe.z > 5.7 && pe.z < 5.91 && inWalls, `z=${pe.z.toFixed(2)} walkable=${inWalls}`)
  // the big doors are open: out onto the loading dock, which ends at its edge
  await page.evaluate(() => window.__factory3d.teleport(7.1, 5.0, Math.PI))
  await hold(['ShiftLeft', 'KeyW'], 3000)
  const pk = await pos()
  check('doors open onto the loading dock (and the dock edge holds)', pk.z > 6.4 && pk.z <= 7.79, `z=${pk.z.toFixed(2)}`)
  await page.evaluate(() => { window.__factory3d.cam.dist = 20; window.__factory3d.cam.pitch = 0.85 })

  // desk: F opens the demo controls; start the p.11 scenario with the same API as DemoStrip
  await page.evaluate(() => window.__factory3d.teleport(7.7, 0, -Math.PI / 2))   // in front of the desk, facing the big screen
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
  // the press 7 lamp is not covered by its tag, by the prompt bubble or by the figure
  const lampCheck = await page.evaluate(() => {
    const f = window.__factory3d, p = f.placed['CURE-07']
    const lamp = f.screenOf(p.x, p.h + 0.54, p.z - p.d * 0.15)
    const head = f.screenOf(f.player.x, 0.95, f.player.z)
    const r = (sel) => { const e = document.querySelector(sel); if (!e) return null; const b = e.getBoundingClientRect(); return [b.left, b.top, b.right, b.bottom] }
    return { lamp, head, tag: r('[data-testid=tag-CURE-07]'), prompt: r('[data-testid=f3-prompt]') }
  })
  const inside = (pt, b) => !!b && pt.x >= b[0] && pt.x <= b[2] && pt.y >= b[1] && pt.y <= b[3]
  const hits = (a, b) => !!a && !!b && a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3]
  check('press 7 lamp clear of its tag and the prompt', !!lampCheck.tag && lampCheck.tag[3] < lampCheck.lamp.y - 4 && !inside(lampCheck.lamp, lampCheck.prompt),
    `lamp ${lampCheck.lamp.x.toFixed(0)},${lampCheck.lamp.y.toFixed(0)} tag ${lampCheck.tag?.map((v) => v.toFixed(0))} prompt ${lampCheck.prompt?.map((v) => v.toFixed(0))}`)
  check('press 7 lamp clear of the figure', Math.hypot(lampCheck.lamp.x - lampCheck.head.x, lampCheck.lamp.y - lampCheck.head.y) > 40,
    `lamp-head ${Math.hypot(lampCheck.lamp.x - lampCheck.head.x, lampCheck.lamp.y - lampCheck.head.y).toFixed(0)} px`)
  check('prompt bubble does not cover the tag', !hits(lampCheck.tag, lampCheck.prompt))
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
  // the approval toast must not sit on the HUD chips
  await mp.evaluate(() => window.__factory3d.toast())
  await sleep(600)
  const clash = await mp.evaluate(() => {
    const r = (sel) => { const e = document.querySelector(sel); if (!e) return null; const b = e.getBoundingClientRect(); return [b.left, b.top, b.right, b.bottom] }
    const t = r('[data-testid=f3-toast]')
    const hit = (a, b) => !!a && !!b && a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3]
    return { t, bad: ['.f3-hud-top', '[data-testid=f3-event]', '[data-testid=f3-pending]'].filter((s) => hit(t, r(s))) }
  })
  check('390 px: toast clear of the HUD', !!clash.t && clash.bad.length === 0, `toast ${clash.t?.map((v) => v.toFixed(0))} overlaps ${clash.bad.join(', ') || 'nothing'}`)
  await mp.screenshot({ path: `${OUT}/factory-390-toast.png` })
  await m.close()
} finally {
  const real = errors.filter((e) => !/favicon/.test(e))
  check('no console errors', real.length === 0, real.slice(0, 5).join(' | '))
  await browser.close()
}
console.log(failed ? `${failed} FAILED` : 'ALL PASS')
process.exit(failed ? 1 : 0)
