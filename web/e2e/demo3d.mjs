// Headless check of the 3D factory's guided demo (▶ Demo（6 分鐘）), the pitch whiteboard and the patrol robot,
// against a running server (any LLM mode).
//   node web/e2e/demo3d.mjs http://localhost:8170            quick (~2 min): button, cues, keys, pitch, slides, robot
//   node web/e2e/demo3d.mjs http://localhost:8170 --full     the real 6 minutes: approves the card at the phone cue
//                                                           (as the presenter would), logs when each cue fired and why
//   --out DIR   where screenshots go (default web/e2e/screenshots)
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
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
const args = process.argv.slice(2)
const BASE = args.find((a) => a.startsWith('http')) ?? 'http://localhost:8000'
const FULL = args.includes('--full')
const oi = args.indexOf('--out')
const OUT = oi >= 0 ? resolve(args[oi + 1]) : resolve(dirname(fileURLToPath(import.meta.url)), 'screenshots')
mkdirSync(OUT, { recursive: true })

let failed = 0
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
async function until(fn, ms, step = 400) {
  const t0 = Date.now()
  while (Date.now() - t0 < ms) { const v = await fn(); if (v) return v; await sleep(step) }
  return null
}
const post = (path, body) => fetch(`${BASE}${path}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })

const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] })
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-TW', timezoneId: 'Asia/Taipei' })
const page = await ctx.newPage()
const errors = []
page.on('pageerror', (e) => errors.push(String(e)))
page.on('console', (m) => { if (m.type() === 'error' && !/favicon|WebGL|GPU stall|ERR_ABORTED/.test(m.text())) errors.push(m.text()) })
const popups = []
ctx.on('page', (p) => popups.push(p))
const demo = () => page.evaluate(() => window.__demo3d?.())
const bot = () => page.evaluate(() => window.__robot3d?.())
const shot = (name) => page.screenshot({ path: resolve(OUT, `${name}.png`) })

// a calm plant to start from (no open event, so the robot patrols)
await post('/api/sim/control', { action: 'scenario', scenario: 'normal', speed: 1 })
await post('/api/sim/control', { action: 'play' })
await page.goto(`${BASE}/factory?test=1`)
await page.waitForSelector('[data-testid="f3-conn"].live', { timeout: 30000 })
await sleep(1500)

// robot exists and patrols (moves along the aisles) before anything happens
const r0 = await bot()
check('robot: hook present', !!r0, JSON.stringify(r0))
await sleep(4000)
const r1 = await bot()
check('robot: patrols (moved)', r0 && r1 && Math.hypot(r1.x - r0.x, r1.z - r0.z) > 0.5, `${r0?.x?.toFixed(2)},${r0?.z} → ${r1?.x?.toFixed(2)},${r1?.z}`)
check('robot: on an aisle or corridor', r1 && ([0.9, 3.2].some((z) => Math.abs(r1.z - z) < 0.06) || [4.2, -7.45].some((x) => Math.abs(r1.x - x) < 0.06)), `${r1?.x},${r1?.z}`)
const inspected = await until(async () => (await bot())?.inspecting, 60000, 300)
check('robot: stops and inspects (arm up, light on)', !!inspected)
if (inspected) { await sleep(1300); const b = await bot(); check('robot: arm raised at a stop', b.arm[1] < 1.6, JSON.stringify(b.arm)) }

// the button on the start screen and in the HUD
check('demo button on the start card', await page.locator('[data-testid="demo-start-intro"]').isVisible())
check('demo button in the HUD', await page.locator('[data-testid="demo-start"]').isVisible())
await shot('demo-00-button')

const t0 = Date.now()
await page.locator('[data-testid="demo-start-intro"]').click()
await page.waitForSelector('[data-testid="demo-cue"]', { timeout: 10000 })
const d0 = await demo()
check('demo: running, first cue', d0?.running && d0.cue === 'start', JSON.stringify({ running: d0?.running, cue: d0?.cue }))
check('demo: camera director on, overview', d0?.director.on && d0.director.dist > 15)
check('demo: start card hidden, keys strip hidden', !(await page.locator('[data-testid="f3-start"]').count()) && !(await page.locator('.f3-keys').count()))
check('demo: first cue text', (await page.locator('[data-testid="demo-cue"]').innerText()).includes('一天開始'))
check('demo: bar shows the next cue', (await page.locator('[data-testid="demo-bar"]').innerText()).includes('下一步'))
await sleep(2500)
const fb = (await demo()).fallback
console.log(`      scenario: ${fb ? 'fallback (normal → press7_temp_drop at 1:02)' : 'morning_rush'}`)
await shot('demo-01-start')

if (!FULL) {
  // B pauses the clock; N skips; D still strafes (no clash): the demo ignores it
  await page.keyboard.press('KeyB')
  const p1 = await demo(); await sleep(1200); const p2 = await demo()
  check('B pauses the demo clock', p1.paused && Math.abs(p2.t - p1.t) < 0.3, `${p1.t} → ${p2.t}`)
  await page.keyboard.press('KeyB')
  await sleep(600)
  check('B resumes', !(await demo()).paused)
  await page.keyboard.press('KeyN')
  await sleep(500)
  check('N → 主控台 cue', (await demo()).cue === 'console')
  check('主控台 cue has an 開啟 button', await page.locator('[data-testid="demo-open-console"]').isVisible())
  await page.locator('[data-testid="demo-open-console"]').click()
  const pop = await until(async () => popups.find((p) => p.url().includes('#agent')), 5000, 200)
  check('開啟 opens the console in a new tab', !!pop, popups.map((p) => p.url()).join(' '))
  await shot('demo-02-console')
  for (const want of ['event', 'phone', 'worker', 'agent', 'pitch']) {
    await page.keyboard.press('KeyN'); await sleep(350); check(`N → ${want}`, (await demo()).cue === want)
    if (want === 'phone') check('phone cue: 拿起手機 + 開啟手機頁', await page.locator('[data-testid="demo-raise-phone"]').isVisible() && await page.locator('[data-testid="demo-open-phone"]').isVisible())
  }
  const dp = await until(async () => { const d = await demo(); return d.t >= 180 ? d : null }, 3000, 150) ?? await demo()
  check('skip into the pitch jumps the clock to 3:00', dp.t >= 180 && dp.t < 186, String(dp.t))
  check('full-screen pitch cue', await page.locator('[data-testid="demo-cue-big"]').isVisible())
  await shot('demo-07-pitch-big')
  await sleep(4600)
  check('pitch: camera on the whiteboard', (await demo()).director.dist < 5)
  check('pitch: countdown shows', (await page.locator('[data-testid="demo-pitch-clock"]').innerText()).includes('Pitch 剩'))
  await shot('demo-08-whiteboard')
  await page.keyboard.press('ArrowRight'); await sleep(300)
  check('→ next slide', (await demo()).slide === 1)
  await page.keyboard.press('ArrowLeft'); await sleep(300)
  check('← previous slide', (await demo()).slide === 0)
  // click the board (centre of the screen in the whiteboard shot) → full-screen slide view
  await page.mouse.click(720, 500)
  const sv = await until(async () => (await demo()).slideView, 3000, 200)
  check('click the whiteboard → full-screen slides', !!sv)
  if (sv) {
    await shot('demo-09-slides-full')
    await page.keyboard.press('ArrowRight'); await sleep(300)
    check('slide view: → works', (await demo()).slide === 1)
    await page.keyboard.press('Escape'); await sleep(300)
    check('Esc closes the slide view, demo still on', !(await demo()).slideView && (await demo()).running)
  }
  await page.keyboard.press('KeyN'); await sleep(400)
  check('N → 剩 1 分鐘', (await demo()).cue === 'warn' && (await demo()).t >= 300)
  check('warn clock turns red', await page.locator('.f3-pitch-clock.warn').isVisible())
  await shot('demo-10-warn')
  await page.keyboard.press('KeyN'); await sleep(400)
  check('N → 結束', (await demo()).cue === 'end')
  await shot('demo-11-end')
  await page.locator('[data-testid="demo-stop"]').click(); await sleep(400)
  check('✕ stops the demo, director off', !(await demo()).running && !(await demo()).director.on)

  // robot at an incident: load the 7 號機 scenario and wait for it to park and point
  await post('/api/sim/control', { action: 'scenario', scenario: 'press7_temp_drop', speed: 10, preroll: true })
  await post('/api/sim/control', { action: 'play' })
  const parked = await until(async () => { const b = await bot(); return b?.mode === 'parked' ? b : null }, 90000, 500)
  check('robot: diverts to the incident machine and parks', !!parked, JSON.stringify(parked))
  if (parked) {
    await until(async () => (await bot()).arm[0] > 0.45, 8000, 300)
    const b = await bot()
    check('robot: arm pointing, scan light on', b.light > 0.8 && b.arm[0] > 0.4, JSON.stringify({ arm: b.arm, light: b.light }))
    // overview camera on the robot for the screenshot
    await page.evaluate(({ x, z }) => { const f = window.__factory3d; f.view.mode = 'third'; f.teleport(x, z - 1.4, Math.PI) }, parked)
    await page.keyboard.press('KeyV'); await sleep(2500)
    await shot('demo-robot-incident')
  }
} else {
  // the real 6 minutes: approve each new card a few seconds after it shows (as CJ would on the phone)
  const shots = new Set()
  const approved = new Set()
  while (true) {
    const d = await demo()
    if (!d?.running) break
    if (!shots.has(d.cue)) {
      shots.add(d.cue)
      await sleep(d.cue === 'pitch' || d.cue === 'end' ? 600 : 2200)   // big cue mid-animation / camera mid-flight
      const i = ['start', 'console', 'event', 'phone', 'worker', 'agent', 'pitch', 'warn', 'end'].indexOf(d.cue)
      await shot(`demo-full-${String(i).padStart(2, '0')}-${d.cue}`)
      if (d.cue === 'pitch') { await sleep(4800); await shot('demo-full-06b-whiteboard') }
      if (d.cue === 'end') break
    }
    const pend = await fetch(`${BASE}/api/approvals?status=pending`).then((r) => r.json()).catch(() => [])
    for (const a of Array.isArray(pend) ? pend : []) {
      if (approved.has(a.request_id)) continue
      if (!a.__seen) { a.__seen = Date.now() }
      approved.add(a.request_id)
      await sleep(6000)
      const opt = a.options?.find((o) => o.recommended)?.option_id ?? a.options?.[0]?.option_id ?? null
      const r = await post(`/api/approvals/${a.request_id}/decision`, { request_id: a.request_id, decision: 'approve', option_id: opt, via: 'phone', reason: 'demo3d e2e' })
      console.log(`      ${((Date.now() - t0) / 1000).toFixed(0)}s approved ${a.request_id} → ${r.status}`)
    }
    const b = await bot()
    if (b?.mode === 'parked' && !shots.has('robot')) { shots.add('robot'); await shot('demo-full-robot-parked') }
    await sleep(700)
  }
  const d = await demo()
  const rows = d.log.map((r) => ({ cue: r.id, planned: r.planned, actual: r.t, reason: r.reason, title: r.title }))
  console.table(rows)
  writeFileSync(resolve(OUT, 'demo-full-log.json'), JSON.stringify({ base: BASE, fallback: d.fallback, seen: d.seen, rows }, null, 2))
  check('full: every cue fired', rows.length === 9, rows.map((r) => r.cue).join(','))
  const pitch = rows.find((r) => r.cue === 'pitch')
  check('full: pitch at 3:00', pitch && Math.abs(pitch.actual - 180) < 1.5, JSON.stringify(pitch))
}

// the fallback run asks the server for morning_rush first (422 until that scenario lands)
const real = errors.filter((e) => !(fb && /422/.test(e)))
check('no page errors', real.length === 0, real.slice(0, 3).join(' | '))
await browser.close()
console.log(failed ? `\n${failed} FAILED` : '\nall passed')
process.exit(failed ? 1 : 0)
