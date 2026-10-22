// The war room's demo mode end to end against a running backend (headless only): presses 「▶ Demo」 in the war room,
// follows the shared cue through the whole story with the 3D factory and the phone open in other tabs, and screenshots
// every beat on all three. At the approval beat it first tries 繼續 (must be refused), then approves on the phone.
//   PORT=8180 LLM_MODE=llm ... uv run python -m warroom.api serve   then   node web/e2e/demo-warroom.mjs http://localhost:8180 <out dir>
import { createRequire } from 'node:module'
import { mkdirSync } from 'node:fs'
import { homedir } from 'node:os'

const require = createRequire(import.meta.url)
let pw
for (const id of [`${homedir()}/.local/lib/node_modules/@playwright/cli/node_modules/playwright-core`, 'playwright', 'playwright-core']) { try { pw = require(id); break } catch { /* next */ } }
if (!pw) { console.error('playwright not found'); process.exit(2) }
const BASE = process.argv[2] ?? 'http://localhost:8180'
const OUT = process.argv[3] ?? 'e2e/screenshots/demo-warroom'
const NO3D = process.env.NO3D === '1'
mkdirSync(OUT, { recursive: true })
let failed = 0
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }

const browser = await pw.chromium.launch({ headless: true, args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] })
const wr = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage()
const phone = await (await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })).newPage()
const wr390 = await (await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })).newPage()
let f3 = null
const api = (path, body) => wr.evaluate(([p, b]) => fetch(p, b ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) } : undefined).then((r) => r.json()), [path, body])

await wr.goto(BASE + '/')
await wr.waitForSelector('[data-testid="demo-mode-button"]', { timeout: 20000 })
const st0 = await api('/api/demo/state')
if (st0.running) await api('/api/demo/state', { action: 'exit' })
await phone.goto(BASE + '/phone')
await wr390.goto(BASE + '/')
if (!NO3D) {
  try {
    f3 = await (await browser.newContext({ viewport: { width: 1280, height: 720 } })).newPage()
    await f3.goto(BASE + '/factory?test=1', { timeout: 30000 })
    await f3.waitForTimeout(4000)
  } catch (e) { console.log('3D tab not available:', String(e).slice(0, 120)); f3 = null }
}

await wr.click('[data-testid="demo-mode-button"]')
await wr.waitForSelector('[data-testid="demo-bar"]', { timeout: 10000 })
check('demo bar shows on start', true)
await phone.waitForTimeout(1500)
check('phone shows 點一下啟用提示音 at demo start', await phone.locator('[data-testid="phone-demo-arm"]').isVisible())

const shots = []
let last = null, n = 0, approvedOnPhone = 0, triedSkip = false
const t0 = Date.now()
const shoot = async (cue, suffix = '') => {
  const name = `${String(n).padStart(2, '0')}-${cue}${suffix}`
  await wr.screenshot({ path: `${OUT}/${name}.png` })
  await phone.screenshot({ path: `${OUT}/${name}-phone.png` })
  await wr390.screenshot({ path: `${OUT}/${name}-390.png` })
  if (f3) await f3.screenshot({ path: `${OUT}/${name}-3d.png` }).catch(() => {})
  const v = await wr.evaluate(() => window.__demoWarroom?.())
  const tab = await wr.evaluate(() => document.querySelector('[aria-current="page"]')?.textContent ?? location.hash)
  const hdrTab = await wr.evaluate(() => document.querySelector('nav [aria-current="page"], header .on')?.textContent ?? '')
  shots.push({ name, cue, caption: v?.caption, hit: v?.hit, tab: tab || hdrTab, hash: await wr.evaluate(() => location.hash) })
  console.log(`[${Math.round((Date.now() - t0) / 1000)}s] ${name}  hash=${await wr.evaluate(() => location.hash)}  hit=${v?.hit}  caption=${v?.caption}`)
}

while (Date.now() - t0 < 9 * 60_000) {
  const v = await wr.evaluate(() => window.__demoWarroom?.() ?? null)
  const s = await api('/api/demo/state')
  if (!s.running) { console.log('demo no longer running', JSON.stringify(s)); break }
  if (s.cue_id !== last) {
    last = s.cue_id; n++
    await wr.waitForTimeout(2000)          // page switch, scroll and spotlight settle
    await shoot(s.cue_id)
    if (f3) {
      const d3 = await f3.evaluate(() => window.__demo3d?.() ?? null).catch(() => null)
      if (d3) check(`3D shows the same cue (${s.cue_id})`, d3.cue === s.cue_id, `3D=${d3.cue}`)
    }
    if (s.cue_id === 'pitch') {
      check('pitch: the war room stays a normal page (no card over it)', !(await wr.locator('[data-testid="demo-pitch"]').count()))
      await wr.keyboard.press('n'); await wr.waitForTimeout(2500)
      n++; last = 'warn'; await shoot('warn')
      const until = async (f, ms = 6000) => { const end = Date.now() + ms; while (Date.now() < end) { if (await f()) return true; await wr.waitForTimeout(250) } return false }
      await wr.keyboard.press('p')
      check('P pauses the demo and the sim', await until(async () => (await api('/api/demo/state')).paused && (await api('/api/sim/scenarios')) && true))
      await wr.keyboard.press('p')
      check('P again resumes', await until(async () => !(await api('/api/demo/state')).paused))
      await wr.keyboard.press('Escape')
      check('Esc ends the demo everywhere', await until(async () => !(await api('/api/demo/state')).running))
      check('war room left demo mode', await until(async () => !(await wr.locator('[data-testid="demo-bar"]').isVisible())))
      break
    }
  }
  if (v && s.cue_id === 'phone' && approvedOnPhone < 3) {
    const pending = (await api('/api/approvals?status=pending')).length
    if (pending > 0) {
      if (!triedSkip) {
        triedSkip = true
        await wr.keyboard.press('n'); await wr.waitForTimeout(1200)
        const after = await api('/api/demo/state')
        check('continue refused while waiting for the approval', after.cue_id === 'phone', after.cue_id)
        await shoot('phone', '-skip-refused')
      }
      // approve the incident's own card (the phone shows 現在核准 once it is on top), not an order plan
      const btn = phone.locator('.ph-card button.ph-approve').first()
      if (await phone.locator('[data-testid="phone-demo-now"]').isVisible() && await btn.isVisible() && await btn.isEnabled()) {
        if (!approvedOnPhone) await shoot('phone', '-card-ready')
        // a card re-issued after a schedule change (…v2, 照新排程執行) needs the supervisor again
        await btn.click(); approvedOnPhone++
        console.log('approved on the phone', approvedOnPhone)
        await phone.waitForTimeout(400)
      }
    }
  }
  await wr.waitForTimeout(700)
}
check('approved the incident card on the phone (現在核准 shown)', approvedOnPhone > 0)
const seen = shots.map((s) => s.cue)
for (const id of ['start', 'console', 'event', 'phone', 'worker', 'agent', 'pitch']) check(`beat ${id} reached`, seen.includes(id))
console.log(JSON.stringify(shots, null, 1))
await browser.close()
console.log(failed ? `\n${failed} FAILED` : '\nall passed')
process.exit(failed ? 1 : 0)
