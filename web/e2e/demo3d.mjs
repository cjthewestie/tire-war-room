// Headless check of the 3D factory's guided demo (▶ Demo（6 分鐘）), the pitch whiteboard, the patrol robot and the
// warning lights, against a running server.
//   node web/e2e/demo3d.mjs http://localhost:8170          quick (~2 min): robot patrol, demo start, checkpoint ① waits
//                                                         with the sim paused, the camera lock, continue, exit confirm
//   node web/e2e/demo3d.mjs http://localhost:8170 --full   the whole story on the real model, presenter simulated:
//                                                         continue 3 s after each checkpoint is ready, each card
//                                                         approved 4 s after it shows (as CJ on the phone); a
//                                                         screenshot every 2 s, a webm, and the cue log
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
const getJSON = (path) => fetch(`${BASE}${path}`).then((r) => r.json()).catch(() => null)

// a calm plant to start from (no open event, so the robot patrols); no demo left running from an earlier run
await post('/api/demo/state', { action: 'exit' }).catch(() => {})
await post('/api/sim/control', { action: 'scenario', scenario: 'normal', speed: 1 })
await post('/api/sim/control', { action: 'play' })

const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] })
const VIEW = FULL ? { width: 1280, height: 800 } : { width: 1440, height: 900 }
const ctx = await browser.newContext({ viewport: VIEW, locale: 'zh-TW', timezoneId: 'Asia/Taipei', ...(FULL ? { recordVideo: { dir: OUT, size: { width: 960, height: 600 } } } : {}) })
const page = await ctx.newPage()
const errors = []
page.on('pageerror', (e) => errors.push(String(e)))
page.on('console', (m) => { if (m.type() === 'error' && !/favicon|WebGL|GPU stall|ERR_ABORTED|409/.test(m.text())) errors.push(m.text()) })
const demo = () => page.evaluate(() => window.__demo3d?.())
const bot = () => page.evaluate(() => window.__robot3d?.())
const shot = (name) => page.screenshot({ path: resolve(OUT, `${name}.png`) })

await page.goto(`${BASE}/factory?test=1`)
await page.waitForSelector('[data-testid="f3-conn"].live', { timeout: 30000 })
await page.waitForFunction(() => !!window.__robot3d && !!window.__demo3d)
await sleep(1500)

if (!FULL) {
  const r0 = await bot(); await sleep(5000); const r1 = await bot()
  check('robot: patrols', Math.hypot(r1.x - r0.x, r1.z - r0.z) > 0.15, `${r0.x.toFixed(2)},${r0.z} → ${r1.x.toFixed(2)},${r1.z}`)
}

/** No cuts: the camera never jumps between two frames while the demo runs (speed between samples < 45 units/s). */
async function camCheck() {
  const trace = await page.evaluate(() => { cancelAnimationFrame(window.__camRaf); return window.__camTrace ?? [] })
  let maxV = 0, at = 0
  for (let i = 1; i < trace.length; i++) {
    const [ta, xa, ya, za] = trace[i - 1], [tb, xb, yb, zb] = trace[i]
    const v = Math.hypot(xb - xa, yb - ya, zb - za) / Math.max((tb - ta) / 1000, 0.05)
    if (v > maxV) { maxV = v; at = (tb - trace[0][0]) / 1000 }
  }
  check('one take: no camera cut while the demo ran', maxV < 45 && trace.length > 10, `max ${maxV.toFixed(1)} u/s at ${at.toFixed(1)} s over ${trace.length} frames`)
}

// start from the intro card
check('demo button on the start card and in the HUD', await page.locator('[data-testid="demo-start-intro"]').isVisible() && await page.locator('[data-testid="demo-start"]').isVisible())
const t0 = Date.now()
// camera samples in the page (every frame), to prove the take has no cuts
await page.evaluate(() => {
  const w = window
  w.__camTrace = []
  const loop = () => { const p = w.__factory3d?.cameraPos?.(); if (p) w.__camTrace.push([performance.now(), ...p]); w.__camRaf = requestAnimationFrame(loop) }
  w.__camRaf = requestAnimationFrame(loop)
})
await page.locator('[data-testid="demo-start-intro"]').click()
await page.waitForSelector('[data-testid="demo-cue"]', { timeout: 15000 })
check('badge 「🎬 Demo 模式・視角鎖定」', (await page.locator('[data-testid="demo-badge"]').innerText()).includes('視角鎖定'))
const d0 = await demo()
check('demo running on ① 一天開始', d0.running && d0.cue === 'start', JSON.stringify({ cue: d0.cue }))

if (!FULL) {
  // the lock: WASD and the mouse do nothing to the lead or the camera's plan
  const p0 = await page.evaluate(() => ({ ...window.__factory3d.player }))
  await page.keyboard.down('KeyW'); await page.keyboard.down('KeyD'); await sleep(1500); await page.keyboard.up('KeyW'); await page.keyboard.up('KeyD')
  await page.mouse.move(700, 450); await page.mouse.down(); await page.mouse.move(300, 300, { steps: 5 }); await page.mouse.up(); await page.mouse.wheel(0, 800)
  const p1 = await page.evaluate(() => ({ ...window.__factory3d.player }))
  check('locked: WASD does not move the lead', Math.hypot(p1.x - p0.x, p1.z - p0.z) < 0.01, `${p0.x},${p0.z} → ${p1.x},${p1.z}`)
  check('locked: V / T / digits do nothing', (await page.keyboard.press('KeyT'), !(await page.locator('[data-testid="f3-anomaly-menu"]').count())))
  // ① waits for continue once the shot has arrived and the orders are in; the sim is paused meanwhile
  const w1 = await until(async () => { const d = await demo(); return d.waiting === 'continue' ? d : null }, 25000, 300)
  check('① waits for continue (按 A 繼續)', !!w1 && await page.locator('[data-testid="demo-continue-prompt"]').isVisible())
  const held = await until(async () => (await getJSON('/api/demo/state'))?.waiting_for === 'continue' && (await demo()).simHeld, 5000, 300)
  check('① sim paused while waiting (server says waiting_for continue)', !!held)
  check('HUD shows 已暫停 while the checkpoint waits', await until(() => page.locator('[data-testid="f3-paused"]').isVisible(), 12000, 400))
  await shot('demo-01-checkpoint')
  await sleep(2500)
  check('① still waiting without a press', (await demo()).cue === 'start')
  await page.keyboard.press('Space')
  const r = await until(async () => { const d = await demo(); return d.cue === 'rush' ? d : null }, 6000, 200)
  check('Space continues → 先核准急單', !!r)
  check('sim plays again after continue', await until(async () => !(await demo()).simHeld && !(await page.locator('[data-testid="f3-paused"]').count()), 5000, 300))
  // the rush checkpoint is an approval: continue is refused while the card waits
  const card = await until(async () => { const p = await getJSON('/api/approvals?status=pending'); return Array.isArray(p) && p.find((a) => a.event_id.endsWith('-RUSH')) }, 60000, 1000)
  check('the 急單 card comes', !!card)
  if (card) {
    await page.keyboard.press('KeyN'); await sleep(1200)
    check('N cannot skip the approval', (await demo()).cue === 'rush' && (await page.locator('[data-testid="demo-bar"]').innerText()).includes('核准'))
    await shot('demo-02-rush-wait')
  }
  await camCheck()
  // exit: Esc asks, Enter leaves; the lead walks again
  await page.keyboard.press('Escape')
  check('Esc asks 「離開 Demo？」', await page.locator('[data-testid="demo-confirm-exit"]').isVisible())
  await page.keyboard.press('Enter')
  check('Enter leaves the demo, camera handed back', !!(await until(async () => { const x = await demo(); return !x.running && !x.director }, 10000, 300)))
  check('server demo ended', (await getJSON('/api/demo/state'))?.running === false)
  const q0 = await page.evaluate(() => ({ ...window.__factory3d.player }))
  await page.keyboard.down('KeyW'); await sleep(4000); await page.keyboard.up('KeyW')
  const q1 = await page.evaluate(() => ({ ...window.__factory3d.player }))
  check('after exit: WASD moves the lead again', Math.hypot(q1.x - q0.x, q1.z - q0.z) > 0.05, `${q0.x.toFixed(2)},${q0.z.toFixed(2)} → ${q1.x.toFixed(2)},${q1.z.toFixed(2)}`)
  // warning lights: 3 件同時異常 → beacons, labels, arrows
  await post('/api/sim/control', { action: 'scenario', scenario: 'concurrent_3', speed: 5, preroll: true })
  await post('/api/sim/control', { action: 'play' })
  const tags = await until(async () => { const n = await page.locator('[data-testid^="f3-alarm-"]').count(); return n >= 2 ? n : null }, 60000, 1000)
  check('warning labels over the abnormal machines', !!tags, String(tags))
  // the wide overview (the demo left the view in the overview)
  await page.evaluate(() => { const f = window.__factory3d; f.teleport(1.6, 0.6, 0, 0.5); f.cam.dist = 30; f.cam.pitch = 0.9 })
  await sleep(3000)
  await shot('alarms-overview-after')
  await page.evaluate(() => { const f = window.__factory3d; f.teleport(10.5, 0, -Math.PI / 2); f.cam.dist = 5 })
  await sleep(2500)
  check('off-screen arrows when the machines are out of view', await page.locator('[data-testid^="f3-arrow-"]:visible').count() > 0)
  await shot('alarms-offscreen-arrows')
} else {
  // the whole story: CJ simulated (continue 3 s after each checkpoint is ready, approve 4 s after a card shows)
  const seenAt = new Map(), approved = new Set()
  let readyAt = null, lastShot = 0, n = 0, lastCue = ''
  const cueShots = new Set()
  while (Date.now() - t0 < 9 * 60 * 1000) {
    const d = await demo()
    if (!d?.running) break
    const now = Date.now()
    if (now - lastShot >= 2000) { lastShot = now; await page.screenshot({ path: resolve(OUT, `seq-${String(n++).padStart(3, '0')}.jpg`), type: 'jpeg', quality: 55 }) }
    if (d.cue !== lastCue) { lastCue = d.cue; readyAt = null }
    if (!cueShots.has(d.cue) && !d.flying && d.waiting !== null) { cueShots.add(d.cue); await shot(`cue-${d.index.toString().padStart(2, '0')}-${d.cue}`) }
    if (d.cue === 'robot' && !cueShots.has('robot-mid') && (await bot())?.playing === 'wave') { cueShots.add('robot-mid'); await shot('cue-07b-robot-wave') }
    if (d.cue === 'robot' && !cueShots.has('robot-scan') && (await bot())?.playing === 'scan') { cueShots.add('robot-scan'); await shot('cue-07c-robot-scan') }
    if (d.cue === 'end') { await sleep(1500); await shot('cue-10-end'); break }
    // checkpoints: continue 3 s after ready; in the pitch, page through the slides on their seconds (sped up 8×)
    if (d.waiting === 'continue') {
      readyAt ??= now
      if (now - readyAt > 2500) { await page.keyboard.press(d.cue === 'pitch' ? 'Space' : 'KeyN'); readyAt = null }
    } else readyAt = null
    if (d.cue === 'warn' || (d.cue === 'pitch' && d.released)) { if (n % 4 === 0) await page.keyboard.press('ArrowRight') }
    const pend = await getJSON('/api/approvals?status=pending')
    for (const a of Array.isArray(pend) ? pend : []) {
      if (approved.has(a.id)) continue
      if (!seenAt.has(a.id)) seenAt.set(a.id, now)
      if (now - seenAt.get(a.id) < 4000) continue
      approved.add(a.id)
      const opt = a.options?.find((o) => o.recommended)?.id ?? a.options?.[0]?.id ?? null
      const r = await post(`/api/approvals/${a.id}/decision`, { request_id: a.id, decision: 'approve', option_id: opt, via: 'phone', reason: 'demo3d e2e' })
      console.log(`      ${((now - t0) / 1000).toFixed(0)}s approved ${a.id} → ${r.status}`)
    }
    await sleep(150)
  }
  await camCheck()
  const d = await demo()
  const rows = (d.log ?? []).map((r) => ({ cue: r.id, planned: r.planned, in: r.t, inBy: r.reason, out: r.out ?? null, outBy: r.outBy ?? null }))
  console.table(rows)
  console.log('      signals first seen (demo s):', JSON.stringify(d.signals))
  console.log('      robot show:', JSON.stringify(d.robotPlayed))
  writeFileSync(resolve(OUT, 'demo-full-log.json'), JSON.stringify({ base: BASE, fallback: d.fallback, seen: d.seen, signals: d.signals, robot: d.robotPlayed, rows }, null, 2))
  check('full: every cue fired', rows.length === 11, rows.map((r) => r.cue).join(','))
  check('full: robot show played turn → wave → scan → bow', (d.robotPlayed ?? []).join(',') === 'turn,wave,scan,bow', JSON.stringify(d.robotPlayed))
  const w = rows.find((r) => r.cue === 'worker')
  check('full: worker beat entered on the real completion', w?.inBy === 'event', JSON.stringify(w))
}

check('no page errors', errors.length === 0, errors.slice(0, 3).join(' | '))
await ctx.close()
await browser.close()
console.log(failed ? `\n${failed} FAILED` : '\nall passed')
process.exit(failed ? 1 : 0)
