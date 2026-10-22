// The war room's strip (two big buttons, the old controls behind ⚙ 展示控制), html[data-demo] and the Xbox pad (a fake
// gamepad: A continues, Menu pauses), against a running backend (LLM_MODE=mock is enough). Headless only.
//   node web/e2e/demo-pad.mjs http://localhost:8180 <out dir>
import { createRequire } from 'node:module'
import { mkdirSync } from 'node:fs'
import { homedir } from 'node:os'

const require = createRequire(import.meta.url)
let pw
for (const id of [`${homedir()}/.local/lib/node_modules/@playwright/cli/node_modules/playwright-core`, 'playwright', 'playwright-core']) { try { pw = require(id); break } catch { /* next */ } }
const BASE = process.argv[2] ?? 'http://localhost:8180'
const OUT = process.argv[3] ?? 'e2e/screenshots/demo-warroom'
mkdirSync(OUT, { recursive: true })
let failed = 0
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }

const browser = await pw.chromium.launch({ headless: true })
const p = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage()
// a fake standard gamepad the page polls through navigator.getGamepads(); window.__pad(i, down) presses / releases
await p.addInitScript(() => {
  const pad = { id: 'fake Xbox', index: 0, connected: true, mapping: 'standard', axes: [0, 0, 0, 0], buttons: Array.from({ length: 17 }, () => ({ pressed: false, value: 0 })) }
  navigator.getGamepads = () => [pad, null, null, null]
  window.__pad = (i, down) => { pad.buttons[i] = { pressed: down, value: down ? 1 : 0 } }
})
const api = (path, body) => p.evaluate(([u, b]) => fetch(u, b ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) } : undefined).then((r) => r.json()), [path, body])
const until = async (f, ms = 5000) => { const end = Date.now() + ms; while (Date.now() < end) { if (await f()) return true; await p.waitForTimeout(150) } return false }
const tap = async (i) => { await p.evaluate((b) => window.__pad(b, true), i); await p.waitForTimeout(250); await p.evaluate((b) => window.__pad(b, false), i); await p.waitForTimeout(250) }

await p.goto(BASE + '/')
await p.waitForSelector('[data-testid="demo-mode-button"]', { timeout: 20000 })
if ((await api('/api/demo/state')).running) await api('/api/demo/state', { action: 'exit' })
check('strip: Demo and QA buttons', await p.locator('[data-testid="demo-mode-button"]').isVisible() && await p.locator('[data-testid="qa-mode-button"]').isVisible())
check('old controls folded by default', !(await p.locator('.demo button:has-text("10×")').isVisible()))
await p.screenshot({ path: `${OUT}/strip.png`, clip: { x: 0, y: 0, width: 1440, height: 260 } })
await p.click('[data-testid="demo-more"] > summary')
check('⚙ 展示控制 opens scenarios, speed, 重來, network', await p.locator('.demo button:has-text("10×")').isVisible() && await p.locator('.demo button:has-text("重來")').isVisible() && await p.locator('.demo button:has-text("弱網")').isVisible())
await p.screenshot({ path: `${OUT}/strip-open.png`, clip: { x: 0, y: 0, width: 1440, height: 300 } })
await p.click('[data-testid="demo-more"] > summary')

await p.click('[data-testid="demo-mode-button"]')
check('Demo starts; html[data-demo] set', await until(async () => (await p.evaluate(() => document.documentElement.dataset.demo)) === 'start'))
check('the strip is hidden while the demo runs', await until(async () => !(await p.locator('[data-testid="demo-mode-button"]').isVisible())))
const i0 = (await api('/api/demo/state')).cue_index
await tap(0)
check('pad A → continue', await until(async () => (await api('/api/demo/state')).cue_index === i0 + 1), String((await api('/api/demo/state')).cue_index))
check('html[data-demo] follows the cue', (await p.evaluate(() => document.documentElement.dataset.demo)) === (await api('/api/demo/state')).cue_id)
await p.evaluate(() => window.__pad(0, true)); await p.waitForTimeout(600)
const i1 = (await api('/api/demo/state')).cue_index
await p.waitForTimeout(600); await p.evaluate(() => window.__pad(0, false))
check('A held does not repeat', (await api('/api/demo/state')).cue_index === i1, `${i1}`)
await tap(9)
check('pad Menu → pause (sim paused too)', await until(async () => (await api('/api/demo/state')).paused))
await tap(9)
check('pad Menu again → resume', await until(async () => !(await api('/api/demo/state')).paused))
await p.keyboard.press('Escape')
check('Esc ends; html[data-demo] removed', await until(async () => !(await api('/api/demo/state')).running && !(await p.evaluate(() => 'demo' in document.documentElement.dataset))))
await p.click('[data-testid="qa-mode-button"]')
check('QA 模式 → #qa', await until(async () => (await p.evaluate(() => location.hash)) === '#qa'))
await browser.close()
console.log(failed ? `\n${failed} FAILED` : '\nall passed')
process.exit(failed ? 1 : 0)
