// Headless check of the simple 3D demo (/factory?demo=simple; CJ 10-27: 「make it an easy version just demo from bird angle
// then > new order flooded in > leave computer > emrgency no 7 temperature > aprrove the plan > worker finished it, check on
// the patrol little care > back to computer」), against a running server in mock mode (./start.sh, LLM_MODE=mock).
// Walks the 7 beats in CJ's order and saves one screenshot per beat (1440×900) into docs/screenshots/simple-demo/.
//   node web/e2e/demo-simple.mjs http://localhost:8131            (default http://localhost:8000)
//   --out DIR   where screenshots go (default docs/screenshots/simple-demo)
// Headless only. Music and prompter are muted / hidden through their stored switches before the page loads.
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
const args = process.argv.slice(2)
const BASE = args.find((a) => a.startsWith('http')) ?? 'http://localhost:8000'
const oi = args.indexOf('--out')
const here = dirname(fileURLToPath(import.meta.url))
const OUT = oi >= 0 ? resolve(args[oi + 1]) : resolve(here, '../../docs/screenshots/simple-demo')
mkdirSync(OUT, { recursive: true })

let failed = 0
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const post = (path, body) => fetch(`${BASE}${path}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })
const getJSON = (path) => fetch(`${BASE}${path}`).then((r) => r.json()).catch(() => null)

/** CJ's 7 beats → the internal beat (demo/beat.ts) whose first appearance is that beat's screenshot. Beat 6 is two
 *  internal beats (repair, then robot): the shot is taken at the robot, after the worker finished. */
const BEATS = [
  { n: 1, name: 'birdseye', at: 'overview', label: '俯瞰' },
  { n: 2, name: 'orders', at: 'orders', label: '新訂單湧入' },
  { n: 3, name: 'leave', at: 'leave', label: '離開電腦' },
  { n: 4, name: 'emergency-press7', at: 'phone', label: '緊急：7 號機溫降' },
  { n: 5, name: 'approve', at: 'approve', label: '核准方案' },
  { n: 6, name: 'worker-robot', at: 'robot', label: '員工修好・巡檢車', also: 'repair' },
  { n: 7, name: 'back-to-desk', at: 'back', label: '回到電腦' },
]
const ORDER = ['overview', 'walk', 'orders', 'leave', 'phone', 'approve', 'repair', 'robot', 'back']
/** How long after a beat first shows before its screenshot (the camera settles, the caption is up). */
const SETTLE_MS = { overview: 2000, orders: 2500, leave: 2200, phone: 2500, approve: 1500, robot: 3500, back: 2500 }
const TIMEOUT_S = 300

// a calm plant to start from; no demo left running from an earlier run; the approvals granted before this run
await post('/api/demo/state', { action: 'exit' }).catch(() => {})
await post('/api/sim/control', { action: 'scenario', scenario: 'normal', speed: 1 })
await post('/api/sim/control', { action: 'play' })
const approvalsBefore = ((await getJSON('/api/approvals')) ?? []).filter((a) => a.status === 'approved' || a.status === 'modified').map((a) => a.id)

const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] })
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-TW', timezoneId: 'Asia/Taipei' })
// the stored switches the page reads at load: music off (demo/music.ts), prompter hidden (demo/Prompter.tsx)
await ctx.addInitScript(() => { try { localStorage.setItem('tp.music.muted', '1'); localStorage.setItem('tp.prompter.visible', '0') } catch { /* blocked */ } })
const page = await ctx.newPage()
const errors = []
page.on('pageerror', (e) => errors.push(String(e)))
page.on('console', (m) => { if (m.type() === 'error' && !/favicon|WebGL|GPU stall|ERR_ABORTED|409/.test(m.text())) errors.push(m.text()) })
const demo = () => page.evaluate(() => window.__demo3d?.() ?? null)
const beatNow = () => page.evaluate(() => document.documentElement.dataset.demoBeat ?? null)
const caption = () => page.evaluate(() => document.querySelector('[data-testid="auto-cap"]')?.textContent ?? '')
const stageChip = () => page.evaluate(() => document.querySelector('[data-testid="simple-stage"]')?.textContent ?? '')

const t0 = Date.now()
const el = () => ((Date.now() - t0) / 1000).toFixed(1)
await page.goto(`${BASE}/factory?demo=simple&test=1`)
await page.waitForSelector('[data-testid="f3-conn"].live', { timeout: 30000 })
await page.waitForFunction(() => !!window.__demo3d, null, { timeout: 30000 })
const started = await page.waitForFunction(() => window.__demo3d?.()?.running === true, null, { timeout: 20000 }).then(() => true, () => false)
check('simple demo starts by itself (?demo=simple)', started)
const d0 = await demo()
check('auto run on the real backend (approval through the decide API, not the mock)', !!d0 && d0.auto === true && d0.autoApprove === false, JSON.stringify({ auto: d0?.auto, autoApprove: d0?.autoApprove }))

// walk the beats: note the first time each internal beat shows, screenshot CJ's 7, record the captions
const seen = {}       // internal beat → demo-clock seconds first seen
const captions = {}
const shots = {}
let last = null
let pendingShot = null   // { beat, due }
while ((Date.now() - t0) / 1000 < TIMEOUT_S) {
  const b = await beatNow()
  if (b && b !== last) {
    last = b
    if (seen[b] == null) {
      seen[b] = Number(el())
      const cap = await caption()
      captions[b] = cap
      console.log(`  ${el().padStart(6)}s  beat ${b}  「${cap}」`)
      const spec = BEATS.find((x) => x.at === b)
      if (spec) pendingShot = { spec, due: Date.now() + SETTLE_MS[b] }
    }
  }
  // beat 7's shot waits for the run to end at the desk (the lead sat down, the 主控台 is up), not the walk back
  if (pendingShot?.spec.at === 'back' && Date.now() >= pendingShot.due && (await demo())?.running !== false && Date.now() < pendingShot.due + 25000) { await sleep(250); continue }
  if (pendingShot?.spec.at === 'back' && Date.now() >= pendingShot.due && !pendingShot.settled) { pendingShot.settled = true; pendingShot.due = Date.now() + 1500 }
  if (pendingShot && Date.now() >= pendingShot.due) {
    const { spec } = pendingShot
    pendingShot = null
    const file = resolve(OUT, `${spec.n}-${spec.name}.png`)
    await page.screenshot({ path: file })
    shots[spec.at] = file
    console.log(`  ${el().padStart(6)}s  📸 ${spec.n}-${spec.name}.png`)
  }
  if (seen.back != null && !pendingShot) break
  const d = await demo()
  if (d && d.running === false && seen.back == null && last !== 'back' && !pendingShot) {
    // the run ended without reaching the desk (an exit, a stop): give the beat store a moment, then stop waiting
    await sleep(1500)
    if ((await beatNow()) !== 'back') break
  }
  await sleep(250)
}

// ---- the report: one line per beat ----
console.log('')
for (const spec of BEATS) {
  const t = seen[spec.at]
  const alsoOk = !spec.also || seen[spec.also] != null
  const prev = BEATS[spec.n - 2]
  const inOrder = !prev || (t != null && seen[prev.at] != null && t >= seen[prev.at])
  const ok = t != null && alsoOk && inOrder && !!shots[spec.at]
  const why = t == null ? 'never shown' : !alsoOk ? `${spec.also} never shown` : !inOrder ? 'out of order' : !shots[spec.at] ? 'no screenshot' : `${t}s`
  check(`beat ${spec.n} ${spec.label}`, ok, why)
}
const order = ORDER.filter((b) => seen[b] != null)
check('internal beats in CJ\'s order', order.every((b, i) => i === 0 || seen[b] >= seen[order[i - 1]]), order.map((b) => `${b}@${seen[b]}`).join(' → '))
check('every beat carries the simple caption (SIMPLE_TEXT), not the 6-minute one', Object.values(captions).every((c) => c && !/手機卡號＝主控台任務編號|實心胎製程的主戰場/.test(c)), JSON.stringify(captions))
check('the n/7 stage chip runs 1 → 7', /^1\/7/.test(captions.overview ?? '') && /^7\/7/.test(captions.back ?? ''), `${captions.overview?.slice(0, 3)} … ${captions.back?.slice(0, 3)}`)
check('no prompter, no dock, no pitch on screen', await page.evaluate(() => !document.querySelector('.tp-box') && !document.querySelector('[data-testid="demo-dock"]') && !document.querySelector('[data-testid="slides-open"]')))
check('no pitch cue reached', !(await demo())?.log?.some?.((r) => r.id === 'pitch'))

// the approval was real: this run granted a new approval for the press-7 event (not the order plan) through the decide API
// (/api/approvals/{id}/decide, as the supervisor's phone does): decided_by 主管, via phone, so it is in the audit chain
const approvals = (await getJSON('/api/approvals')) ?? []
const fresh = approvals.filter((a) => (a.status === 'approved' || a.status === 'modified') && !approvalsBefore.includes(a.id))
const press7 = fresh.filter((a) => !/-(RUSH|PLAN)(-\d+)?$/.test(a.event_id))
check('press-7 card approved through the decide API (decided_by 主管/supervisor, via phone)', press7.length >= 1 && press7.every((a) => /^(主管|supervisor)$/.test(a.decision?.decided_by ?? '') && a.decision?.via === 'phone'), `${press7.length} approved: ${press7.map((a) => `${a.id} (${a.event_id}) by ${a.decision?.decided_by} via ${a.decision?.via}`).join(', ')}`)
const orderPlan = fresh.filter((a) => /-(RUSH|PLAN)(-\d+)?$/.test(a.event_id))
check('the order plan approved on the console (beat 2)', orderPlan.length >= 1, orderPlan.map((a) => `${a.id} via ${a.decision?.via}`).join(', '))
check('no page errors', errors.length === 0, errors.slice(0, 3).join(' | '))

await ctx.close()
await browser.close()
console.log(`\n${failed ? `${failed} FAILED` : 'all passed'}; screenshots in ${OUT}`)
process.exit(failed ? 1 : 0)
