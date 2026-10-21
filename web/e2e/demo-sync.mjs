// The guided demo on two screens at once, against a running backend (headless only): the 3D factory starts it (its
// driver), the war room follows the shared cue (/api/demo/state). At every beat it records the server's cue, the 3D's
// cue and the war room's page / spotlight / caption, and screenshots all screens. The war room presses 繼續 at continue
// checkpoints (the 3D must follow), tries it at approvals (must be refused), approves the 急單 card on the war room and
// the 7 號機 cards on /phone, checks 「討論」 brings the chat input into view (390 wide), and ends with Esc.
//   node web/e2e/demo-sync.mjs http://localhost:8180 <out dir>
import { createRequire } from 'node:module'
import { mkdirSync } from 'node:fs'
import { homedir } from 'node:os'

const require = createRequire(import.meta.url)
let pw
for (const id of [`${homedir()}/.local/lib/node_modules/@playwright/cli/node_modules/playwright-core`, 'playwright', 'playwright-core']) { try { pw = require(id); break } catch { /* next */ } }
if (!pw) { console.error('playwright not found'); process.exit(2) }
const BASE = process.argv[2] ?? 'http://localhost:8180'
const OUT = process.argv[3] ?? 'e2e/screenshots/demo-sync'
mkdirSync(OUT, { recursive: true })
let failed = 0
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }

const browser = await pw.chromium.launch({ headless: true, args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] })
const f3 = await (await browser.newContext({ viewport: { width: 1280, height: 720 } })).newPage()
const wr = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage()
const phone = await (await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })).newPage()
const wr390 = await (await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })).newPage()
const api = (path, body) => wr.evaluate(([p, b]) => fetch(p, b ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) } : undefined).then((r) => r.json()), [path, body])
const d3 = () => f3.evaluate(() => window.__demo3d?.() ?? null).catch(() => null)

await wr.goto(BASE + '/')
await wr.waitForSelector('[data-testid="demo-mode-button"]', { timeout: 20000 })
if ((await api('/api/demo/state')).running) await api('/api/demo/state', { action: 'exit' })
await phone.goto(BASE + '/phone')
await wr390.goto(BASE + '/')
await f3.goto(BASE + '/factory?test=1', { timeout: 45000 })
await f3.waitForTimeout(4000)
const intro = f3.locator('[data-testid="demo-start-intro"]')
if (await intro.isVisible().catch(() => false)) await intro.click(); else await f3.click('[data-testid="demo-start"]')
const started = await (async () => { for (let i = 0; i < 40; i++) { const s = await api('/api/demo/state'); if (s.running) return s; await wr.waitForTimeout(500) } return null })()
check('3D Demo starts the shared demo (driver 3d)', !!started && started.driver === '3d', started?.driver)

const rows = []
let last = null, n = 0, cueSeenAt = 0, triedSkip = '', discussed = false, approvals = 0
const t0 = Date.now()
const shoot = async (cue, suffix = '') => {
  const name = `${String(n).padStart(2, '0')}-${cue}${suffix}`
  await wr.screenshot({ path: `${OUT}/${name}-warroom.png` })
  await f3.screenshot({ path: `${OUT}/${name}-3d.png` }).catch(() => {})
  await phone.screenshot({ path: `${OUT}/${name}-phone.png` })
  return name
}
while (Date.now() - t0 < 8 * 60_000) {
  const s = await api('/api/demo/state')
  if (!s.running) { console.log('demo ended', s.cue_id); break }
  if (s.cue_id !== last) {
    last = s.cue_id; n++; cueSeenAt = Date.now()
    await wr.waitForTimeout(2000)
    const x = await d3(), v = await wr.evaluate(() => window.__demoWarroom?.() ?? null)
    const hash = await wr.evaluate(() => location.hash)
    const name = await shoot(s.cue_id)
    const row = { sec: Math.round((Date.now() - t0) / 1000), server: s.cue_id, d3: x?.cue ?? null, wait: s.waiting_for, warroom: { hash, caption: v?.caption, hit: v?.hit }, shot: name }
    rows.push(row)
    console.log(`[${row.sec}s] server=${row.server} 3d=${row.d3} wait=${row.wait} warroom ${hash || '#主控台'} hit=${v?.hit} caption=${v?.caption}`)
    if (s.cue_id === 'pitch') {
      check('pitch: war room shows the calm card', await wr.locator('[data-testid="demo-pitch"]').isVisible())
      await wr.keyboard.press('Escape')
      let ended = false
      for (let i = 0; i < 20 && !ended; i++) { await wr.waitForTimeout(300); ended = !(await api('/api/demo/state')).running }
      check('Esc on the war room ends the demo', ended)
      await wr.waitForTimeout(1500)
      const after = await d3()
      check('… and the 3D stops too', !after?.running, JSON.stringify(after?.running))
      break
    }
  }
  // 「討論」 on a narrow screen: the chat input comes into view with the caret (restored #101 fix)
  if (!discussed && (s.cue_id === 'rush' || s.cue_id === 'h1')) {
    const btn = wr390.locator('[data-testid="visual-discuss"], [data-testid="plan-discuss"]').first()
    if (await btn.isVisible().catch(() => false)) {
      discussed = true
      await btn.click(); await wr390.waitForTimeout(900)
      const r = await wr390.evaluate(() => { const el = document.querySelector('[data-testid="chat-input"]'); if (!el) return null; const b = el.getBoundingClientRect(); return { focused: document.activeElement === el, inView: b.top >= 0 && b.bottom <= innerHeight } })
      check('討論 (390 wide): chat input focused and in view', !!r?.focused && !!r?.inView, JSON.stringify(r))
      await wr390.screenshot({ path: `${OUT}/discuss-390.png` })
    }
  }
  if (s.waiting_for === 'continue' && Date.now() - cueSeenAt > 3000) {
    const before = s.cue_index
    await wr.keyboard.press('n')
    await wr.waitForTimeout(1500)
    const x = await d3()
    console.log(`   war room 繼續 at ${s.cue_id}: server ${before} → ${(await api('/api/demo/state')).cue_index}, 3D ${x?.cue} (released ${x?.released})`)
    cueSeenAt = Date.now()
  }
  if (s.waiting_for === 'approval') {
    if (triedSkip !== s.cue_id) {
      triedSkip = s.cue_id
      await wr.keyboard.press('n'); await wr.waitForTimeout(1000)
      const a = await api('/api/demo/state')
      check(`繼續 refused at the ${s.cue_id} approval`, a.cue_id === s.cue_id && a.cue_index === s.cue_index, a.cue_id)
    }
    const now = phone.locator('[data-testid="phone-demo-now"]')
    const btn = phone.locator('.ph-card button.ph-approve').first()
    if (await now.isVisible().catch(() => false) && await btn.isEnabled().catch(() => false)) {
      await shoot(s.cue_id, '-card'); await btn.click(); approvals++
      console.log(`   approved on /phone (${s.cue_id})`); await phone.waitForTimeout(1500)
    } else if (s.cue_id === 'rush' && Date.now() - cueSeenAt > 4000) {
      const wbtn = wr.locator('[data-testid="visual-approve"]').first()
      if (await wbtn.isVisible().catch(() => false) && await wbtn.isEnabled().catch(() => false)) {
        await shoot(s.cue_id, '-card'); await wbtn.click(); approvals++
        console.log('   approved 急單 on the war room'); await wr.waitForTimeout(1500)
      }
    }
  }
  // 3D and war room agree on the cue (both read the shared pointer; the 3D drives it)
  await wr.waitForTimeout(600)
}
const agree = rows.filter((r) => r.server === r.d3).length
check(`3D on the server's cue at each beat (${agree}/${rows.length})`, agree === rows.length, rows.filter((r) => r.server !== r.d3).map((r) => `${r.server}≠${r.d3}`).join(', '))
for (const id of ['start', 'rush', 'console', 'event', 'h1', 'verify', 'h2', 'worker', 'agent', 'robot', 'pitch']) check(`beat ${id} reached`, rows.some((r) => r.server === id))
check('approved cards during the demo', approvals > 0, String(approvals))
console.log(JSON.stringify(rows, null, 1))
if ((await api('/api/demo/state')).running) await api('/api/demo/state', { action: 'exit' })
await browser.close()
console.log(failed ? `\n${failed} FAILED` : '\nall passed')
process.exit(failed ? 1 : 0)
