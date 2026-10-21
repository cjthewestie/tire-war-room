// Headless check of the issue #17 plant scenarios against a running FastAPI server (mock mode).
//   uv run python -m warroom.api serve --port 8091 &   then   node web/e2e/scenarios.mjs http://127.0.0.1:8091
// For each of 早班開工 / 插單 / 人力調度: load it, wait for the plant event's approval card, approve the recommended
// option, then follow the worker tasks the approval became (待派 → 前往中 → 執行中 → 完成) until the event closes.
// 插單: the goods queue before / after approval (the rush order moves into it); dashboard 工廠全貌 + agent console with
// the event's tasks at 1440 and 390 while tasks run. 3D: rings above machines and task markers on walking workers in
// the overview; a scenario started from the dashboard inside the 3D desk console.
// Screenshots -> web/e2e/screenshots/scenarios-*.png, factory-rings-*.png
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
const BASE = process.argv[2] ?? 'http://127.0.0.1:8091'
const ONLY = process.argv[3]   // optional: one scenario name
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
const api = (path, data) => fetch(`${BASE}${path}`, data ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) } : undefined).then((r) => r.json())
const hm = (iso) => (iso ? iso.slice(11, 19) : '—')

// Headless (the default). SwiftShader gives WebGL without a GPU.
const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] })
const errors = []
const watch = (page) => {
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
  page.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`) })
}

async function plantEvent() {
  const evs = await api('/api/events')
  return evs.find((e) => e.source === 'plant') ?? null
}

/** Load, approve, follow the tasks. onRunning(ev) is called once while some task is 前往中/執行中. */
async function run(name, speed, onRunning) {
  console.log(`\n=== ${name} ===`)
  await api('/api/sim/control', { action: 'scenario', scenario: name })
  await api('/api/sim/control', { action: 'speed', speed })
  const waiting = await until(async () => { const e = await plantEvent(); return e && e.state === 'await_approval' ? e : null }, 180000, 500)
  check(`${name}: plant event reaches await_approval`, !!waiting, waiting ? `${waiting.id} ${waiting.anomaly.message}` : 'timeout')
  if (!waiting) return
  const pend = (await api('/api/approvals?status=pending')).filter((p) => p.event_id === waiting.id)
  const req = pend[0]
  const opt = req.options.find((o) => o.recommended) ?? req.options[0]
  console.log(`INFO  card ${req.id} ${req.risk}「${req.title}」 已做：${req.done.join('；')}`)
  console.log(`INFO  card 交期：${req.delivery_impact}`)
  console.log(`INFO  card options: ${req.options.map((o) => `${o.id} ${o.label}${o.recommended ? '（建議）' : ''}`).join(' / ')}`)
  const before = (await api('/api/world')).goods.items.map((g) => `${g.order_id}:${g.status}`)
  const res = await api(`/api/approvals/${req.id}/decision`, { request_id: req.id, decision: 'approve', option_id: opt.id, via: 'dashboard', reason: 'e2e scenarios' })
  check(`${name}: approved ${opt.id} ${opt.label}`, res.status === 'approved', res.status)
  const seen = new Map()
  let shot = false
  const closed = await until(async () => {
    const [w, e] = await Promise.all([api('/api/world'), api(`/api/events/${waiting.id}`)])
    for (const t of w.tasks.items.filter((x) => x.event_id === waiting.id)) {
      if (seen.get(t.id) !== t.status) {
        seen.set(t.id, t.status)
        console.log(`TASK  ${hm(w.factory_time)} ${t.id} ${t.status} ${t.kind}・${t.worker_name ?? '—'}・${t.where}・${t.title}`)
      }
    }
    if (!shot && onRunning && w.tasks.items.some((t) => t.event_id === waiting.id && (t.status === '前往中' || t.status === '執行中'))) {
      shot = true
      await onRunning(waiting, before)
    }
    return e.state === 'closed' || e.state === 'safe_stop' ? e : null
  }, 240000, 300)
  const w = await api('/api/world')
  const mine = w.tasks.items.filter((t) => t.event_id === waiting.id)
  check(`${name}: tasks went 前往中 → 執行中 → 完成`, mine.length > 0 && mine.every((t) => t.status === '完成' && t.assigned_at && t.started_at && t.done_at),
    mine.map((t) => `${t.id} ${t.kind} ${hm(t.assigned_at)}→${hm(t.started_at)}→${hm(t.done_at)}`).join('; '))
  check(`${name}: event closed after the tasks`, closed?.state === 'closed', closed ? closed.outcome : 'timeout')
  const verify = closed?.timeline.filter((x) => x.actor === '驗證（程式）').map((x) => `${hm(x.ts)} ${x.text}`) ?? []
  console.log(`INFO  verify: ${verify.join(' | ')}`)
  const audit = await api('/api/audit?limit=2000')
  const taskAudit = audit.filter((a) => a.kind === 'task' && a.event_id === waiting.id)
  check(`${name}: task changes audited`, taskAudit.length >= mine.length * 3, `${taskAudit.length} task audit entries; e.g. ${taskAudit.slice(0, 2).map((a) => `${a.actor}:${a.payload.change ?? 'created'} ${a.payload.task_id ?? (a.payload.created ?? []).map((c) => c.id).join(',')}`).join(' / ')}`)
  return { ev: waiting, before, after: w.goods.items.map((g) => `${g.order_id}:${g.status}`) }
}

try {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-TW', timezoneId: 'Asia/Taipei' })
  const page = await ctx.newPage()
  watch(page)
  const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, locale: 'zh-TW', timezoneId: 'Asia/Taipei', isMobile: true, hasTouch: true })
  const p3 = await phone.newPage()
  watch(p3)

  for (const [name, speed] of [['shift_start', 60], ['staffing', 60], ['rush_order', 10]]) {
    if (ONLY && ONLY !== name) continue
    const isRush = name === 'rush_order'
    const r = await run(name, speed, isRush ? async () => {
      // tasks are running: dashboard 工廠全貌 + agent console, 1440 and 390
      await page.goto(`${BASE}/?live=1#world`, { waitUntil: 'domcontentloaded' })
      // UX declutter: goods and tasks sit in 「更多全廠資訊」 (closed by default); on a phone the plant section folds too
      await page.waitForSelector('[data-testid=plant-more]', { timeout: 15000 })
      await page.evaluate(() => document.querySelectorAll('[data-testid=plant-panels] details').forEach((d) => { d.open = true }))
      await page.waitForSelector('[data-testid=world-goods] [data-testid=goods-item]', { timeout: 15000 })
      await sleep(800)
      const rows = await page.locator('[data-testid=world-tasks] [data-testid=task-row]').evaluateAll((els) => els.map((e) => e.dataset.status))
      check('1440 工廠全貌: task rows live', rows.length > 0, rows.join(','))
      check('1440 工廠全貌: order rings', (await page.locator('[data-testid=order-ring]').count()) > 0)
      const rushItems = await page.locator('[data-testid=goods-item][data-order="#R-009"]').evaluateAll((els) => els.map((e) => e.dataset.status))
      check('1440 工廠全貌: rush tyres in the goods queue', rushItems.length > 0, rushItems.join(','))
      await page.screenshot({ path: `${OUT}/scenarios-1440-world.png`, fullPage: true })
      await page.goto(`${BASE}/?live=1`, { waitUntil: 'domcontentloaded' })
      // home (#16): ④ 執行中 items carry the task progress; selecting one shows its TaskList under the plan board
      await until(() => page.locator('.hl-item', { hasText: '任務' }).count(), 15000)
      await page.locator('.hl-item', { hasText: '任務' }).first().click().catch(() => {})
      const et = await until(() => page.locator('[data-testid=event-tasks] [data-testid=task-row]').count(), 15000)
      check('1440 agent console: event TaskList', !!et, `${et} rows`)
      await page.screenshot({ path: `${OUT}/scenarios-1440-console.png` })
      await p3.goto(`${BASE}/?live=1#world`, { waitUntil: 'domcontentloaded' })
      await p3.waitForSelector('[data-testid=plant-more]', { state: 'attached', timeout: 15000 })
      await p3.evaluate(() => document.querySelectorAll('[data-testid=plant-panels] details').forEach((d) => { d.open = true }))
      await p3.waitForSelector('[data-testid=world-tasks]', { timeout: 15000 })
      await sleep(600)
      const sw = await p3.evaluate(() => document.documentElement.scrollWidth)
      check('390 工廠全貌: no sideways scroll', sw <= 392, `scrollWidth ${sw}`)
      await p3.locator('[data-testid=world-tasks]').scrollIntoViewIfNeeded()
      await p3.screenshot({ path: `${OUT}/scenarios-390-world-tasks.png` })
      await p3.locator('[data-testid=world-goods]').scrollIntoViewIfNeeded()
      await p3.screenshot({ path: `${OUT}/scenarios-390-world-goods.png` })
      await p3.goto(`${BASE}/?live=1`, { waitUntil: 'domcontentloaded' })
      await until(() => p3.locator('.hl-item', { hasText: '任務' }).count(), 15000)
      await p3.locator('.hl-item', { hasText: '任務' }).first().click().catch(() => {})
      await until(() => p3.locator('[data-testid=event-tasks]').count(), 15000)
      await p3.locator('[data-testid=event-tasks]').scrollIntoViewIfNeeded().catch(() => {})
      await p3.screenshot({ path: `${OUT}/scenarios-390-console.png` })
    } : null)
    if (isRush && r) {
      const pos = (arr) => arr.findIndex((x) => x.startsWith('#R-009'))
      console.log(`QUEUE before approval (${r.before.length}): ${r.before.join(' ')}`)
      console.log(`QUEUE after  approval (${r.after.length}): ${r.after.join(' ')}`)
      check('rush order reorders the goods queue', pos(r.before) === -1 && pos(r.after) >= 0, `#R-009 at ${pos(r.before)} -> ${pos(r.after)}`)
    }
  }

  // ---------------------------------------------------------------- 3D: rings + task workers, desk console start
  if (!ONLY || ONLY === '3d') {
    console.log('\n=== 3D ===')
    await api('/api/sim/control', { action: 'scenario', scenario: 'shift_start' })
    await api('/api/sim/control', { action: 'speed', speed: 60 })
    const w3 = await ctx.newPage()
    watch(w3)
    await w3.goto(`${BASE}/factory?test=1`, { waitUntil: 'domcontentloaded' })
    await w3.waitForSelector('.f3-canvas canvas', { timeout: 15000 })
    await until(() => w3.evaluate(() => !!window.__goods3d && window.__goods3d().length > 0 && !!window.__world3d && !!window.__patrol3d), 20000)
    await w3.click('[data-testid=f3-start]')
    await sleep(2500)
    await w3.evaluate(() => window.__factory3d.teleport(1.8, 0.8, Math.PI / 2))
    await w3.keyboard.press('KeyV')
    await until(() => w3.evaluate(() => window.__factory3d.fov() < 33), 15000, 300)
    // CJ 2026-10-20 「恢復人力待命 有一兩個人巡邏」: before any task, everyone stands by and only the patrol walks
    const idle = await w3.evaluate(() => ({ task: window.__world3d.walkers().length, patrol: window.__patrol3d() }))
    check('3D: no task -> nobody walks except the patrol', idle.task === 0 && idle.patrol >= 1 && idle.patrol <= 2, `task walkers ${idle.task}, patrol ${idle.patrol}`)
    await w3.evaluate(() => { const s = document.querySelector('[data-testid=f3-start]'); if (s) s.style.visibility = 'hidden' })
    await w3.screenshot({ path: `${OUT}/factory-rings-standby.png` })
    // approve the 今日計畫 so the warm-up / 補料 tasks send people walking
    const ev = await until(async () => { const e = await plantEvent(); return e && e.state === 'await_approval' ? e : null }, 120000, 500)
    if (ev) {
      const req = (await api('/api/approvals?status=pending')).find((p) => p.event_id === ev.id)
      await api(`/api/approvals/${req.id}/decision`, { request_id: req.id, decision: 'approve', option_id: (req.options.find((o) => o.recommended) ?? req.options[0]).id, via: 'dashboard' })
      await api('/api/sim/control', { action: 'speed', speed: 5 })
    }
    const onTask = await until(() => w3.evaluate(() => { const ws = window.__world3d.walkers().filter((x) => x.task && x.walking); return ws.length ? ws : null }), 60000, 200)
    check('3D: a task worker leaves standby with a marker', !!onTask, onTask ? onTask.map((x) => `${x.id} ${x.task} ${x.task_id} from (${x.x.toFixed(1)},${x.z.toFixed(1)})`).join(', ') : 'none')
    const wTask = await api('/api/world')
    const assigned = new Set(wTask.tasks.items.filter((t) => t.status === '前往中' || t.status === '執行中').map((t) => t.worker_id))
    const only = await w3.evaluate(() => window.__world3d.walkers().map((x) => x.id))
    check('3D: only task assignees left standby', only.length > 0 && only.every((id) => assigned.has(id)), `walkers ${only.join(',')} / assignees ${[...assigned].join(',')}`)
    // SwiftShader renders a few frames a second and a frame's step is capped, so figures walk slowly here (wall time)
    const fps = await w3.evaluate(() => new Promise((r) => { let n = 0; const t0 = performance.now(); const f = () => { n++; if (performance.now() - t0 < 2000) requestAnimationFrame(f); else r(n / 2) }; requestAnimationFrame(f) }))
    console.log(`INFO  fps headless SwiftShader: ${fps}`)
    const who = onTask?.[0]?.id
    const there = await until(() => w3.evaluate((id) => { const x = window.__world3d.walkers().find((k) => k.id === id); return x && x.phase === 'at' && Math.hypot(x.x - x.target[0], x.z - x.target[1]) < 0.05 ? x : null }, who), 240000, 200)
    check('3D: the assignee walks to the task place', !!there, there ? `${who} at (${there.x.toFixed(2)},${there.z.toFixed(2)}) ${there.key} ring ${Math.round(there.progress * 100)}%` : 'not there')
    const rings = await w3.evaluate(() => window.__goods3d())
    const byStatus = rings.reduce((a, r) => { a[r.status] = (a[r.status] ?? 0) + 1; return a }, {})
    check('3D: goods rings above machines', rings.length > 0, `${rings.length} rings ${JSON.stringify(byStatus)}; e.g. ${rings.slice(0, 3).map((r) => `${r.machine} ${r.status} ${Math.round(r.progress * 100)}%`).join(', ')}`)
    await w3.screenshot({ path: `${OUT}/factory-rings-overview.png` })
    await w3.screenshot({ path: `${OUT}/factory-rings-workers.png` })
    // task done -> walks back into standby and leaves the scene
    await api('/api/sim/control', { action: 'speed', speed: 60 })
    let sawBack = false
    const back = await until(() => w3.evaluate((id) => { const x = window.__world3d.walkers().find((k) => k.id === id); return x ? (x.phase === 'back' ? 'back' : null) : 'gone' }, who).then((v) => { if (v === 'back') sawBack = true; return v === 'gone' ? v : null }), 300000, 150)
    const tDone = (await api('/api/world')).tasks.items.filter((t) => t.worker_id === who && t.status === '完成').map((t) => `${t.id} 完成 ${hm(t.done_at)}`)
    check('3D: after 完成 the assignee walks back to standby', !!back && sawBack, `${who}: ${sawBack ? 'walked back' : 'no back phase seen'}, ${back ?? 'still out'}; ${tDone.join(', ')}`)
    // zoom in on the presses with rings
    await w3.mouse.move(720, 450)
    await w3.mouse.wheel(0, -500)
    await sleep(1200)
    await w3.screenshot({ path: `${OUT}/factory-rings-zoom.png` })
    // back to first person, sit at the desk and start 插單 from the dashboard inside the desk monitor
    await w3.keyboard.press('KeyV')
    await until(() => w3.evaluate(() => window.__factory3d.fov() > 69), 15000, 300)
    await w3.evaluate(() => window.__factory3d.teleport(7.7, 0, -Math.PI / 2))
    await sleep(600)
    await w3.keyboard.press('KeyF')
    await w3.waitForSelector('[data-testid=f3-monitor] iframe', { timeout: 5000 })
    const desk = w3.frameLocator('[data-testid=f3-monitor] iframe')
    await desk.locator('.demo button:has-text("人力調度")').first().waitFor({ timeout: 15000 }).catch(() => {})
    const names = await desk.locator('.demo button').allInnerTexts()
    check('desk console lists the new scenarios', ['早班開工', '插單', '人力調度'].every((n) => names.some((x) => x.includes(n))), names.slice(0, 9).join(' / '))
    await desk.locator('.demo button:has-text("插單")').first().click()
    const started = await until(async () => { const h = await api('/api/health'); return h.factory_time.startsWith('2026-10-07') ? h.factory_time : null }, 20000)
    check('插單 started from the 3D desk console', !!started, started ?? 'clock not on 10/07')
    await w3.screenshot({ path: `${OUT}/factory-rings-desk.png` })
  }
  const real = errors.filter((e) => !/WebSocket|ERR_CONNECTION|favicon|Failed to load resource/.test(e))
  check('no page errors', real.length === 0, real.slice(0, 3).join(' | '))
} catch (e) {
  failed++
  console.log('FAIL  exception', e)
} finally {
  await browser.close()
  console.log(failed ? `\n${failed} FAILED` : '\nALL PASSED')
  process.exit(failed ? 1 : 0)
}
