// Issue #54 check against a live backend serving web/dist (headless):
//   PORT=8104 LLM_MODE=mock uv run python -m warroom.api   then   node web/e2e/orch54.mjs http://localhost:8104 [outDir]
// 主控台: a 下新任務 → 主 agent dispatches a subagent → approval → 主 agent reports back. Agent page: 主 agent → subagent
// tree with status and progress, the model queue, 中斷 on a running subagent. 狀態機: the detector's pre-classification.
// Screenshots at 1440 and 390, light and dark, go to outDir (default docs/screenshots/issue-54).
import { createRequire } from 'node:module'
import { homedir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { mkdirSync } from 'node:fs'

const require = createRequire(import.meta.url)
let pw
for (const id of ['playwright', 'playwright-core', `${homedir()}/.local/lib/node_modules/@playwright/cli/node_modules/playwright-core`]) { try { pw = require(id); break } catch { /* next */ } }
const BASE = process.argv[2] ?? 'http://localhost:8104'
const OUT = process.argv[3] ?? resolve(dirname(fileURLToPath(import.meta.url)), '../../docs/screenshots/issue-54')
mkdirSync(OUT, { recursive: true })
const results = []
const check = (n, ok, d = '') => { results.push(ok); console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${d ? `  — ${d}` : ''}`) }
const errors = []
const b = await pw.chromium.launch({ headless: true })
const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, timezoneId: 'Asia/Taipei' })
const p = await ctx.newPage()
p.on('pageerror', (e) => errors.push(e.message))
const api = (path, body) => p.evaluate(async ([u, bd]) => { const r = await fetch(u, bd === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(bd) }); return { s: r.status, j: await r.json().catch(() => null) } }, [path, body])
const until = async (f, n = 120, ms = 500) => { for (let i = 0; i < n; i++) { if (await f()) return true; await p.waitForTimeout(ms) } return false }
const step = (s = 30) => api('/api/sim/step', { seconds: s })
await p.goto(BASE + '/api/health')
const llm = (await api('/api/health')).j?.llm_mode === 'llm'

await api('/api/sim/control', { action: 'scenario', scenario: 'normal' })
await p.goto(BASE + '/', { waitUntil: 'networkidle' })
await p.waitForSelector('[data-testid="chat-input"]')
check('主控台 conversation is the 主 agent', (await p.locator('.console h2').innerText()).trim() === '主 agent')

// 1. a supervisor task from 主控台 → dispatch row
await p.locator('[data-testid="mode-command"]').click()
await p.locator('[data-testid="chat-input"]').fill('#M-201 可能晚交，請重排保交期')
await p.locator('[data-testid="chat-input"]').press('Enter')
const tr = p.locator('[data-testid="transcript"]')
check('主 agent dispatch message appears', await until(async () => (await tr.locator('[data-testid="main-dispatch"]').count()) >= 1, 120),
  (await tr.locator('[data-testid="main-dispatch"]').first().innerText().catch(() => '')).replace(/\n/g, ' ').slice(0, 120))
const disp = await tr.locator('[data-testid="main-dispatch"]').first().innerText().catch(() => '')
check('it names the subagent (訂單處理) and how it was chosen', disp.includes('已派發子 agent：訂單處理') && /(Nemotron 選派|關鍵字規則)/.test(disp), disp.replace(/\n/g, ' ').slice(0, 100))
const subs = async () => (await api('/api/subagents')).j
let me = null
await until(async () => { me = (await subs()).items.find((x) => x.type === 'order' && x.source === 'supervisor'); return !!me }, 40)
check('GET /api/subagents lists it under 主 agent', !!me && me.parent === 'main', me ? `${me.id} ${me.status}` : '')
for (let i = 0; i < 40; i++) {
  const s = (await subs()).items.find((x) => x.id === me.id)
  if (['waiting_approval', 'done', 'failed'].includes(s.status)) break
  await step(30); await p.waitForTimeout(llm ? 1500 : 300)
}
check('the plan-area item shows which subagent handles it', await until(async () => (await p.locator(`[data-testid="home-item-${me.id}"] [data-testid="item-subagent"]`).count()) === 1, 30),
  await p.locator(`[data-testid="home-item-${me.id}"] [data-testid="item-subagent"]`).innerText().catch(() => ''))
await p.screenshot({ path: `${OUT}/home-dispatch-1440.png` })
const card = (await api('/api/approvals?status=pending')).j?.find((a) => a.event_id === me.id)
if (card) await api(`/api/approvals/${card.id}/decision`, { request_id: card.id, decision: 'approve' })
for (let i = 0; i < 60; i++) {
  const rows = await tr.locator('[data-testid="main-report"]').count()
  if (rows) break
  await step(60); await p.waitForTimeout(llm ? 1000 : 300)
}
const rep = await tr.locator('[data-testid="main-report"]').last().innerText().catch(() => '')
check('主 agent reports back in 主控台 when the subagent ends', rep.includes('訂單處理子 agent'), rep.replace(/\n/g, ' ').slice(0, 160))
await p.screenshot({ path: `${OUT}/home-report-1440.png` })

// 2. Agent page: tree, progress, model queue, 中斷 on a second task
await api('/api/command', { text: '夜班缺一人，請調人並重排' })
let second = null
await until(async () => { second = (await subs()).items.find((x) => x.type === 'staff' && !['done', 'failed', 'interrupted'].includes(x.status)); return !!second }, 40)
// interrupt it while it still works (do not wait for its card: a real-model run may end on its own)
await p.goto(`${BASE}/#console?event=${encodeURIComponent(second.id)}`, { waitUntil: 'networkidle' })
await p.waitForSelector('[data-testid="tree-root"]')
check('Agent page: 主 agent root with subagents under it', (await p.locator('[data-testid="tree-root"]').innerText()).includes('主 agent') && await p.locator('[data-testid="trace-event"]').count() >= 2)
check('each subagent shows its status', await p.locator('[data-testid="sub-status"]').count() >= 2)
check('the model queue is shown', await p.locator('[data-testid="model-queue"]').count() === 1)
check('the selected subagent shows how it was dispatched', (await p.locator('[data-testid="sub-head"]').innerText()).includes('主 agent →'))
await p.screenshot({ path: `${OUT}/agent-tree-1440.png`, fullPage: false })
const s2 = (await subs()).items.find((x) => x.id === second.id)
if (!['done', 'failed', 'interrupted'].includes(s2.status)) {
  p.once('dialog', (d) => d.accept())
  await p.click('[data-testid="interrupt"]')
  check('中斷 stops it: status 已中斷', await until(async () => (await subs()).items.find((x) => x.id === second.id)?.status === 'interrupted', 30))
  check('主 agent writes the interrupt and the report', await until(async () => (await p.locator('[data-testid="trace-body"] [data-testid="main-interrupt"]').count()) >= 1 &&
    (await p.locator('[data-testid="trace-body"] [data-testid="main-report"]').count()) >= 1, 30))
  const ev2 = (await api(`/api/events/${second.id}`)).j
  check('the run ended in SAFE_STOP 「主管中斷」 with no pending card', ev2.state === 'safe_stop' && ev2.outcome.includes('主管中斷') &&
    !(await api('/api/approvals?status=pending')).j.some((a) => a.event_id === second.id), ev2.outcome)
  await p.screenshot({ path: `${OUT}/agent-interrupted-1440.png` })
} else check('second subagent was still running to interrupt', false, s2.status)

// 3. 狀態機: the detector's pre-classification
await api('/api/sim/control', { action: 'scenario', scenario: 'press7_temp_drop' })
for (let i = 0; i < 6; i++) await step(120)
await p.goto(`${BASE}/#sm`, { waitUntil: 'networkidle' })
await p.waitForSelector('[data-testid="sm-preclass"]')
check('事件預分類 lists the anomaly before the LLM', await until(async () => (await p.locator('[data-testid="preclass-item"]').count()) >= 1, 20))
const pre = await p.locator('[data-testid="preclass-item"]').first().innerText().catch(() => '')
check('…with rule, validation, forecast and the routing decision', /規則/.test(pre) && /驗證/.test(pre) && /預測/.test(pre) && pre.includes('機台處理'), pre.replace(/\n/g, ' ').slice(0, 160))
await p.screenshot({ path: `${OUT}/sm-preclass-1440.png`, fullPage: true })
await api('/api/sim/control', { action: 'scenario', scenario: 'bad_data' })
for (let i = 0; i < 4; i++) await step(60)
await p.reload({ waitUntil: 'networkidle' })
check('bad data rejected by validation is listed (never wakes the AI)', await until(async () => (await p.locator('[data-testid="preclass-bad"] li').count()) >= 1, 20))

// screenshots: 主控台, Agent page, 狀態機 at 1440 / 390, light / dark
await api('/api/sim/control', { action: 'scenario', scenario: 'press7_temp_drop' })
for (let i = 0; i < 6; i++) await step(120)
for (const theme of ['light', 'dark']) {
  for (const [w, h] of [[1440, 900], [390, 844]]) {
    const c = await b.newContext({ viewport: { width: w, height: h }, isMobile: w < 500, hasTouch: w < 500 })
    await c.addInitScript((t) => { try { localStorage.setItem('warroom-theme', t) } catch { /* */ } }, theme)
    const s = await c.newPage()
    s.on('pageerror', (e) => errors.push(`${w}/${theme}: ${e.message}`))
    for (const [name, path] of [['home', '/'], ['agent', '/#console'], ['sm', '/#sm']]) {
      await s.goto(BASE + path, { waitUntil: 'networkidle' })
      await s.waitForTimeout(1500)
      const over = await s.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
      if (w < 500) check(`390 no sideways scroll ${name} ${theme}`, over <= 1, `${over}px`)
      await s.screenshot({ path: `${OUT}/${name}-${w}-${theme}.png`, fullPage: w >= 500 && name !== 'home' })
    }
    await c.close()
  }
}
check('no page errors', errors.length === 0, errors.slice(0, 3).join(' | '))
await b.close()
const ok = results.every(Boolean)
console.log(ok ? `ALL PASS (${results.length})` : `${results.filter((x) => !x).length} FAILED of ${results.length}`)
process.exit(ok ? 0 : 1)
