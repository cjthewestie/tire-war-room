// Headless check of the 3D factory (/factory) against a running FastAPI server (./start.sh, mock mode).
//   PORT=8010 ./start.sh &   then   node web/e2e/factory3d.mjs http://localhost:8010
// Checks: canvas renders, no console errors, the start overview shows the whole building, then FIRST PERSON by default
// (eye height, wide lens, head-bob, WASD relative to the look direction, Q/E turn, Shift runs), collision (desk, glass
// wall, outer wall, dock edge) and open doorways; the war-room big screen shows the REAL dashboard page (one iframe);
// F at the desk opens the real dashboard full-size and starting the press-7 scenario from inside it turns press 7 red in
// the 3D world; F at a machine shows its live readings; the doors on the hall walls lead to the separate 休息區 and
// 廁所 spaces (walk in, or F) and back; a new approval raises the toast and the real /phone page approves from inside
// the 休息區; P works in the 廁所 too; V toggles the overview (third person + tilt-shift).
// Screenshots -> web/e2e/screenshots/factory-*.png
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
  // (a previous run may have left the p.11 scenario running at 10×)
  await page.request.post(`${BASE}/api/sim/control`, { data: { action: 'scenario', scenario: 'normal' } })
  await page.request.post(`${BASE}/api/sim/control`, { data: { action: 'speed', speed: 1 } })
  await page.request.post(`${BASE}/api/sim/control`, { data: { action: 'reset' } })
  // ?scenarios=all: the desk screen's 展示列 (loaded below) shows only with it or ?demo=1, the iframe inheriting the
  // factory's query; ?demo=1 would also fold the 3D HUD into the presenter's one answer card (d/3d-polish), and this
  // walk checks the full HUD
  await page.goto(`${BASE}/factory?scenarios=all&test=1`, { waitUntil: 'domcontentloaded' })
  const canvas = await page.waitForSelector('.f3-canvas canvas', { timeout: 15000 })
  await sleep(1000)
  const box = await canvas.boundingBox()
  check('canvas renders full screen', !!box && box.width >= 1400 && box.height >= 850, JSON.stringify(box))
  const gl = await page.evaluate(() => { const c = document.querySelector('canvas'); return !!(c && (c.getContext('webgl2') || c.getContext('webgl'))) })
  check('WebGL context', gl)
  const live = await until(() => page.locator('.f3-conn.live').count(), 10000)
  check('HUD connected to live backend', !!live, await page.locator('.f3-hud-top').innerText())

  // before start: the camera circles the whole building; all four corners and the dock are on screen
  await sleep(2500)
  const corners = () => page.evaluate(() => {
    const f = window.__factory3d, B = { x0: -8.0, x1: 11.6, z0: -6.0, z1: 6.2 }
    const pts = [[B.x0, B.z0], [B.x1, B.z0], [B.x0, B.z1], [B.x1, B.z1], [7.1, 8.0]].map(([x, z]) => f.screenOf(x, 0, z))
    return { dist: f.cam.dist, ok: pts.every((s) => s && s.x > 0 && s.x < innerWidth && s.y > 0 && s.y < innerHeight), pts: pts.map((s) => s ? `${s.x.toFixed(0)},${s.y.toFixed(0)}` : 'behind').join(' ') }
  })
  const pre = await corners()
  check('start overview shows the whole building', pre.ok, `corners+dock ${pre.pts}`)
  await page.evaluate(() => { document.querySelector('[data-testid=f3-start]').style.visibility = 'hidden' })
  await page.screenshot({ path: `${OUT}/factory-overview.png` })
  await page.evaluate(() => { document.querySelector('[data-testid=f3-start]').style.visibility = '' })
  const rooms = await page.evaluate(() => [...document.querySelectorAll('[data-testid^=room-]')].map((e) => e.textContent))
  check('room signs: 戰情室, 員工待命區 + door plates 休息區, 廁所', ['戰情室', '員工待命區', '休息區', '廁所'].every((r) => rooms.some((t) => t.includes(r))), rooms.join(' · '))
  const signs = await page.evaluate(() => [...document.querySelectorAll('[data-testid^=sign-]')].map((e) => e.textContent))
  check('all six line signs show (DOM, unblurred)', signs.length === 6, signs.join(' · '))
  check('break area and restroom are not inside the hall any more', await page.evaluate(() => !('breakArea' in window.__factory3d.rooms) && !('restroom' in window.__factory3d.rooms)))

  // start: first person by default (the camera eases from the overview down to the eyes)
  await page.click('[data-testid=f3-start]')
  const fpOk = await until(() => page.evaluate(() => window.__factory3d.fov() > 69), 20000, 300)
  await sleep(800)
  const eye = await page.evaluate(() => ({ mode: window.__factory3d.view.mode, fov: window.__factory3d.fov(), cam: window.__factory3d.cameraPos(), p: { ...window.__factory3d.player } }))
  check('first person by default (eye height, wide lens)', !!fpOk && eye.mode === 'first' && Math.abs(eye.cam[1] - 0.78) < 0.04 && Math.hypot(eye.cam[0] - eye.p.x, eye.cam[2] - eye.p.z) < 0.05,
    `mode ${eye.mode} fov ${eye.fov?.toFixed(0)} eye y ${eye.cam[1].toFixed(2)} at (${eye.cam[0].toFixed(2)},${eye.cam[2].toFixed(2)})`)
  await page.screenshot({ path: `${OUT}/factory-start.png` })
  const onScreen = await page.evaluate(() => {
    const f = window.__factory3d
    return Object.values(f.placed).filter((p) => { const s = f.screenOf(p.x, p.h / 2, p.z); return s && s.x > 0 && s.x < innerWidth && s.y > 0 && s.y < innerHeight }).length
  })
  check('start view looks across the machine rows', onScreen >= 14, `${onScreen}/28 machines on screen`)
  check('hand phone + aim dot in first person', (await page.locator('.f3-cross').count()) === 1)
  const tilt = await page.evaluate(() => document.querySelector('.f3-keys').innerText)
  check('key legend names V for the overview', tilt.includes('V') && tilt.includes('俯瞰'), tilt.replace(/\n/g, ' '))

  const pos = () => page.evaluate(() => ({ ...window.__factory3d.player }))
  const hold = async (keys, ms) => { for (const k of keys) await page.keyboard.down(k); await sleep(ms); for (const k of [...keys].reverse()) await page.keyboard.up(k) }
  // Q/E turn the lead (no pointer lock needed); W walks where you look
  const y0 = (await pos()).yaw
  await hold(['KeyQ'], 500)
  const y1 = (await pos()).yaw
  check('Q turns the view (no mouse needed)', y1 - y0 > 0.1, `yaw ${y0.toFixed(2)} -> ${y1.toFixed(2)}`)
  await page.evaluate(() => window.__factory3d.teleport(0.5, 1.4, -Math.PI / 2))   // facing +x down the D aisle
  await sleep(300)
  // SwiftShader frame rates swing (the controller caps a frame's step), so walk until the lead has moved, sampling the eye
  const bob = []
  await page.keyboard.down('KeyW')
  await until(async () => { bob.push((await page.evaluate(() => window.__factory3d.cameraPos()))[1]); return (await pos()).x > 1.0 }, 6000, 100)
  await page.keyboard.up('KeyW')
  const w1 = await pos()
  check('W walks the way you look', w1.x - 0.5 > 0.3 && Math.abs(w1.z - 1.4) < 0.05, `(0.50,1.40) -> (${w1.x.toFixed(2)},${w1.z.toFixed(2)})`)
  check('light head-bob while walking', Math.max(...bob) - Math.min(...bob) > 0.003 && Math.max(...bob) < 0.82, `eye y ${Math.min(...bob).toFixed(3)}..${Math.max(...bob).toFixed(3)}`)
  await page.evaluate(() => window.__factory3d.teleport(0.5, 1.4, -Math.PI / 2))
  await hold(['KeyW'], 1000)
  const wd = (await pos()).x - 0.5
  await page.evaluate(() => window.__factory3d.teleport(0.5, 1.4, -Math.PI / 2))
  await hold(['ShiftLeft', 'KeyW'], 1000)
  const rd = (await pos()).x - 0.5
  check('Shift runs faster than walking', rd > wd * 1.3, `walk ${wd.toFixed(2)} vs run ${rd.toFixed(2)}`)
  // collision: walk into the desk from behind it (between the desk and the big screen) should stop short of it
  await page.evaluate(() => window.__factory3d.teleport(9.1, 0, Math.PI / 2))
  await hold(['KeyW'], 1500)
  const pc = await pos()
  check('collision: desk blocks the lead', pc.x > 8.76, `x=${pc.x.toFixed(2)} (desk edge 8.575 + radius 0.22)`)
  await page.evaluate(() => window.__factory3d.teleport(6.8, -1.5, 0))
  await hold(['KeyW'], 1500)
  const pg = await pos()
  check('collision: war-room glass wall blocks', pg.z > -2.13 && pg.z < -1.6, `z=${pg.z.toFixed(2)} (glass at -2.4 + 0.08 + 0.22)`)
  await page.evaluate(() => window.__factory3d.teleport(4.0, 0, -Math.PI / 2))
  await hold(['ShiftLeft', 'KeyW'], 1500)
  const pd = await pos()
  check('war-room doorway is open', pd.x > 5.2, `x=${pd.x.toFixed(2)}`)
  await page.evaluate(() => window.__factory3d.teleport(0, 5.6, Math.PI))
  await hold(['ShiftLeft', 'KeyW'], 2000)
  const pe = await pos()
  const inWalls = await page.evaluate(([x, z]) => window.__factory3d.walkable(x, z, 0.2), [pe.x, pe.z])
  check('collision: the outer wall keeps the lead inside', pe.z > 5.7 && pe.z < 5.91 && inWalls, `z=${pe.z.toFixed(2)} walkable=${inWalls}`)
  await page.evaluate(() => window.__factory3d.teleport(7.1, 5.0, Math.PI))
  await hold(['ShiftLeft', 'KeyW'], 2500)
  const pk = await pos()
  check('doors open onto the loading dock (and the dock edge holds)', pk.z > 6.4 && pk.z <= 7.79, `z=${pk.z.toFixed(2)}`)

  // the big screen on the war-room wall is the REAL dashboard page (one iframe, same origin); it opens on 產線監控,
  // which has its own <h1> since #102: read the header's
  await page.evaluate(() => window.__factory3d.teleport(7.7, 0, -Math.PI / 2))   // in front of the desk, facing the screen
  const big = page.frameLocator('[data-testid=f3-bigscreen] iframe')
  const bigH1 = await big.locator('.topbar h1').innerText({ timeout: 15000 }).catch(() => '')
  check('big screen shows the real dashboard (/)', bigH1.includes('實心胎智慧製造戰情室'), bigH1)
  const bigBox = await until(() => page.locator('[data-testid=f3-bigscreen]').boundingBox(), 8000, 300)
  check('big screen fills the view from the desk', !!bigBox && bigBox.width > 600 && bigBox.x > 0 && bigBox.x + bigBox.width < 1440, JSON.stringify(bigBox && { x: Math.round(bigBox.x), y: Math.round(bigBox.y), w: Math.round(bigBox.width), h: Math.round(bigBox.height) }))
  await sleep(1500)
  await page.screenshot({ path: `${OUT}/factory-bigscreen.png` })
  check('desk prompt', (await page.locator('[data-testid=f3-prompt]').innerText()).includes('坐下使用戰情室電腦'))

  // sit down: F opens the real dashboard full-size; start p.11 from its own 展示 strip
  await page.keyboard.press('KeyF')
  await page.waitForSelector('[data-testid=f3-monitor] iframe', { timeout: 3000 })
  const desk = page.frameLocator('[data-testid=f3-monitor] iframe')
  const deskH1 = await desk.locator('h1').innerText({ timeout: 15000 }).catch(() => '')
  const tabs = await desk.locator('header button, nav button').allInnerTexts().catch(() => [])
  check('F at the desk opens the real dashboard full-size', deskH1.includes('實心胎智慧製造戰情室') && tabs.join(' ').includes('人力排程'), `${deskH1} · ${tabs.slice(0, 4).join(' / ').replace(/\n/g, ' ')}`)
  const frames = await page.evaluate(() => ({ big: document.querySelectorAll('[data-testid=f3-bigscreen] iframe').length, all: [...document.querySelectorAll('iframe')].map((f) => f.getAttribute('src')) }))
  check('one big-screen iframe, one desk iframe, one phone iframe', frames.big === 1 && frames.all.length === 3, frames.all.join(' , '))
  // the scenario buttons sit behind ⚙ 展示控制 (the strip shows only Demo / QA)
  await desk.locator('[data-testid="demo-more"]:not([open]) > summary').click().catch(() => {})
  await desk.locator('.demo button:has-text("7 號硫化機溫降")').click()
  await sleep(600)
  // the 展示列 has no speed buttons since #100: 10× over the API, as the presenter's server setting would
  await page.request.post(`${BASE}/api/sim/control`, { data: { action: 'speed', speed: 10 } })
  await sleep(2500)
  await page.screenshot({ path: `${OUT}/factory-desk.png` })
  await desk.locator('h1').click()   // focus inside the dashboard: Esc there stands up too
  await page.keyboard.press('Escape')
  await sleep(500)
  check('Esc stands up from the desk', (await page.locator('[data-testid=f3-monitor]').count()) === 0)

  const red = await until(async () => {
    const s = await (await page.request.get(`${BASE}/api/snapshot`)).json()
    const m = s.machines.find((x) => x.machine_id === 'CURE-07')
    return m && ['alarm', 'warning', 'stopped', 'out_of_service'].includes(m.status) ? m : null
  }, 90000, 1000)
  check('press 7 goes abnormal (scenario started from the dashboard)', !!red, red ? `${red.status} ${red.temp_c}°C` : 'timeout')
  const lamp3d = await until(() => page.evaluate(() => { const l = window.__factory3d.lights['CURE-07']; return l === 'red' || l === 'amber' ? l : null }), 10000)
  check('press 7 lamp in the 3D world turns red/amber', !!lamp3d, lamp3d ?? String(await page.evaluate(() => window.__factory3d.lights['CURE-07'])))
  await page.evaluate(() => window.__factory3d.standAt('CURE-07'))
  await sleep(1500)
  const tag = page.locator('[data-testid=tag-CURE-07]')
  const tagCls = (await tag.count()) ? await tag.getAttribute('class') : ''
  check('press 7 label shows red/amber + temperature', /red|amber/.test(tagCls ?? ''), `${tagCls} · ${(await tag.count()) ? (await tag.innerText()).replace(/\n/g, ' ') : ''}`)
  const lampCheck = await page.evaluate(() => {
    const f = window.__factory3d, p = f.placed['CURE-07']
    const lamp = f.screenOf(p.x, p.h + 0.54, p.z - p.d * 0.15)
    const r = (sel) => { const e = document.querySelector(sel); if (!e) return null; const b = e.getBoundingClientRect(); return [b.left, b.top, b.right, b.bottom] }
    return { lamp, tag: r('[data-testid=tag-CURE-07]'), prompt: r('[data-testid=f3-prompt]') }
  })
  const inside = (pt, b) => !!pt && !!b && pt.x >= b[0] && pt.x <= b[2] && pt.y >= b[1] && pt.y <= b[3]
  const hits = (a, b) => !!a && !!b && a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3]
  check('press 7 lamp in view, clear of its tag and the prompt', !!lampCheck.lamp && lampCheck.lamp.y > 0 && lampCheck.lamp.y < 900 && !!lampCheck.tag && lampCheck.tag[1] > 0 && lampCheck.tag[3] < lampCheck.lamp.y - 4 && !inside(lampCheck.lamp, lampCheck.prompt),
    `lamp ${lampCheck.lamp?.x.toFixed(0)},${lampCheck.lamp?.y.toFixed(0)} tag ${lampCheck.tag?.map((v) => v.toFixed(0))} prompt ${lampCheck.prompt?.map((v) => v.toFixed(0))}`)
  check('prompt does not cover the tag', !hits(lampCheck.tag, lampCheck.prompt))
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

  // the 休息區 is its own space: walk into its door on the north wall
  const scene = () => page.evaluate(() => window.__factory3d.view.scene)
  await page.evaluate(() => window.__factory3d.teleport(8.0, -4.8, 0))
  await sleep(600)
  check('door prompt at the 休息區 door', (await page.locator('[data-testid=f3-prompt]').innerText().catch(() => '')).includes('進入休息區'))
  await hold(['KeyW'], 1500)
  await sleep(900)
  const inBreak = await scene()
  const bp = await pos()
  check('walking into the door teleports to the 休息區', inBreak === 'break' && (await page.locator('[data-testid=f3-where]').innerText()).includes('休息區'), `scene ${inBreak} at (${bp.x.toFixed(2)},${bp.z.toFixed(2)})`)
  check('the hall is hidden in the 休息區 (signs gone, big screen hidden)', await page.evaluate(() => document.querySelectorAll('[data-testid^=sign-]').length === 0 && getComputedStyle(document.querySelector('[data-testid=f3-bigscreen]')).display === 'none'))
  // collision in the room: the bench stops the lead
  await page.evaluate(() => window.__factory3d.teleport(-2.4, -0.4, Math.PI / 2))
  await hold(['KeyW'], 1200)
  const pb = await pos()
  check('collision in the 休息區 (bench)', pb.x > -3.18 + 0.2 && pb.x < -2.5, `x=${pb.x.toFixed(2)}`)
  await page.evaluate(() => window.__factory3d.teleport(0, 1.9, 0))
  await sleep(1200)
  await page.screenshot({ path: `${OUT}/factory-break.png` })

  // approval arrives while in the 休息區 -> toast; P raises the real phone page; approve from here
  const toast = await until(() => page.locator('[data-testid=f3-toast]').count(), 90000, 1000)
  check('new approval -> HUD toast (in the 休息區)', !!toast, toast ? await page.locator('[data-testid=f3-toast]').innerText() : 'no approval within 90 s')
  check('HUD pending count live in the 休息區', /待核准 [1-9]/.test(await page.locator('[data-testid=f3-pending]').innerText()), await page.locator('[data-testid=f3-pending]').innerText())
  await page.keyboard.press('KeyP')
  await sleep(1200)
  check('P raises the phone (in the 休息區)', (await page.locator('.f3-phone.up').count()) === 1)
  const frame = page.frameLocator('.f3-phone iframe')
  await frame.locator('.phone').waitFor({ timeout: 10000 })
  await page.screenshot({ path: `${OUT}/factory-phone.png` })
  const opt = frame.locator('[role=radio]').first()
  if (await opt.count()) {
    const pendingN = async () => (await (await page.request.get(`${BASE}/api/snapshot`)).json()).pending_approvals.length
    const before = await pendingN()
    await opt.click()
    await frame.locator('button.ph-approve').first().click()
    const after = await until(async () => { const n = await pendingN(); return n < before ? String(n) : null }, 10000)
    check('approve inside the raised phone, from the 休息區', !!after, `pending ${before} -> ${after}`)
  } else check('approval card in phone', false, 'no option radio found')
  const ta = frame.locator('textarea[placeholder*="9 號機"]')
  if (await ta.count()) {
    await ta.fill('7 號硫化機現在溫度多少？')
    await frame.locator('[data-testid=ask-send]').click()
    const ans = await until(async () => { const a = frame.locator('[data-testid=ask-answer]'); return (await a.count()) ? (await a.last().innerText()).replace(/\n/g, ' ').slice(0, 120) : null }, 20000)
    check('問戰情室 answers inside the phone', !!ans, ans ?? 'no answer')
  } else check('問戰情室 box in phone', false)
  await page.screenshot({ path: `${OUT}/factory-phone-used.png` })
  await page.keyboard.press('Escape')
  await sleep(600)
  check('Esc lowers the phone', (await page.locator('.f3-phone.up').count()) === 0)

  // back to the hall through the room's exit door (walk into it)
  await page.evaluate(() => window.__factory3d.teleport(0, 2.3, Math.PI))
  await hold(['KeyW'], 1200)
  await sleep(900)
  const back = await pos()
  check('exit door leads back to the hall, by the 休息區 door', (await scene()) === 'factory' && Math.hypot(back.x - 8.0, back.z + 4.8) < 0.3, `scene ${await scene()} at (${back.x.toFixed(2)},${back.z.toFixed(2)})`)

  // the 廁所: F at its door on the east wall
  await page.evaluate(() => window.__factory3d.teleport(10.9, 4.6, -Math.PI / 2))
  await sleep(600)
  check('door prompt at the 廁所 door', (await page.locator('[data-testid=f3-prompt]').innerText().catch(() => '')).includes('進入廁所'))
  await page.keyboard.press('KeyF')
  await sleep(1200)
  check('F at the door teleports to the 廁所', (await scene()) === 'toilet' && (await page.locator('[data-testid=f3-where]').innerText()).includes('廁所'))
  await page.evaluate(() => window.__factory3d.toast('測試通知', '廁所裡也收得到'))
  await sleep(1500)
  await page.screenshot({ path: `${OUT}/factory-toilet.png` })
  check('toast shows in the 廁所', (await page.locator('[data-testid=f3-toast]').count()) === 1)
  await page.keyboard.press('KeyP')
  await sleep(1000)
  check('P raises the phone in the 廁所', (await page.locator('.f3-phone.up').count()) === 1)
  await page.keyboard.press('Escape')
  await sleep(500)
  await page.evaluate(() => window.__factory3d.teleport(-1.2, 1.2, Math.PI))
  await sleep(500)
  await page.keyboard.press('KeyF')
  await sleep(1200)
  check('the 廁所 exit leads back to the hall', (await scene()) === 'factory')

  // V: the overview (third person, the chibi lead, tilt-shift); V again back to first person
  await page.evaluate(() => window.__factory3d.teleport(1.8, 0.8, Math.PI / 2))
  await page.keyboard.press('KeyV')
  const ov = await until(() => page.evaluate(() => window.__factory3d.fov() < 33 ? window.__factory3d.cameraPos() : null), 15000, 300)
  check('V switches to the overview (third person)', !!ov && ov[1] > 5, ov ? `camera y ${ov[1].toFixed(1)}` : 'no switch')
  await page.mouse.move(720, 450)
  const d0 = await page.evaluate(() => window.__factory3d.cam.dist)
  await page.mouse.wheel(0, 600)
  await sleep(400)
  const d1 = await page.evaluate(() => window.__factory3d.cam.dist)
  check('mouse wheel zooms the overview', d1 > d0 + 1 && d1 <= 36, `dist ${d0.toFixed(1)} -> ${d1.toFixed(1)}`)
  await page.mouse.wheel(0, -600)
  await sleep(1500)
  await page.screenshot({ path: `${OUT}/factory-overview-v.png` })
  await page.keyboard.press('KeyV')
  const fpBack = await until(() => page.evaluate(() => window.__factory3d.fov() > 69), 15000, 300)
  check('V again returns to first person', !!fpBack)

  // fps sample (headless SwiftShader: CPU rendering, far below a real GPU; reported, not asserted)
  const fps = await page.evaluate(() => new Promise((r) => { let n = 0; const t0 = performance.now(); const f = () => { n++; if (performance.now() - t0 < 2000) requestAnimationFrame(f); else r(n / 2) }; requestAnimationFrame(f) }))
  console.log(`INFO  fps headless SwiftShader: ${fps}`)

  // phone screen (390 px wide) — the HUD must still fit. Close the big page first: two SwiftShader pages starve each other.
  await page.close()
  const m = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: 'zh-TW', timezoneId: 'Asia/Taipei', hasTouch: true })
  const mp = await m.newPage()
  mp.on('pageerror', (e) => errors.push(`pageerror(390): ${e.message}`))
  await mp.goto(`${BASE}/factory?test=1`, { waitUntil: 'domcontentloaded' })
  await mp.waitForSelector('canvas', { timeout: 15000 })
  await sleep(1500)
  await mp.click('[data-testid=f3-start]')
  await until(() => mp.evaluate(() => window.__factory3d.fov() > 69), 20000, 300)
  await sleep(800)
  await mp.screenshot({ path: `${OUT}/factory-390.png` })
  const over = await mp.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)
  check('390 px: no horizontal overflow', !over)
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
