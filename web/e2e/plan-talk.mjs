// Issue #10: 討論這個計劃 + 下新任務, end to end against a running backend. Headless only.
//   PORT=8060 ./start.sh   then   node web/e2e/plan-talk.mjs http://localhost:8060
// (a) press-7 scenario: while the approval card waits, type 「9 號機今晚要保養」 in the 主控台's Agent panel →
//     the agent re-plans, card v2 replaces v1 and says what changed (#A-311 no longer on press 9), v1 can
//     no longer be approved, v2 is approved from the card. The phone shows the newest version.
// (b) 下新任務 「通知維修組明早檢查 3 號機」 → a supervisor event goes PLAN → gate → approval → closed,
//     every step in the audit log.
// Writes web/e2e/screenshots/plan-talk-*.png at 1440 and 390 wide.
import { createRequire } from 'node:module'
import { mkdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
let pw
for (const id of ['playwright', 'playwright-core', `${homedir()}/.local/lib/node_modules/@playwright/cli/node_modules/playwright-core`]) { try { pw = require(id); break } catch { /* next */ } }
if (!pw) { console.error('playwright not found (npm i -g @playwright/cli or npx playwright install chromium)'); process.exit(2) }
const BASE = process.argv[2] ?? 'http://localhost:8000'
const WAIT_S = Number(process.env.WAIT_S ?? 240)       // a real model replans slower than the rule mode
const OUT = resolve(dirname(fileURLToPath(import.meta.url)), 'screenshots')
mkdirSync(OUT, { recursive: true })
const results = []
const check = (n, ok, d = '') => { results.push(!!ok); console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${d ? `  — ${d}` : ''}`) }
const errors = []
const api = (page, path, body) => page.evaluate(([p, b]) => fetch(p, b === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) })
  .then(async (x) => ({ status: x.status, json: await x.json().catch(() => null) })), [path, body])
const until = async (page, fn, s = WAIT_S) => { for (let i = 0; i < s * 2; i++) { if (await fn()) return true; await page.waitForTimeout(500) } return false }

const b = await pw.chromium.launch({ headless: true })
try {
  const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, timezoneId: 'Asia/Taipei' })
  const d = await ctx.newPage()
  d.on('pageerror', (e) => errors.push(e.message))
  await d.goto(BASE + '/', { waitUntil: 'networkidle' })
  await d.waitForSelector('.mode.live', { timeout: 15000 }).catch(() => {})
  // the console is the main page (CJ, 2026-10-20): `/` opens it, it is the first tab, with the plant strip on top
  const firstTab = await d.locator('.tabs [role=tab]').first().innerText().catch(() => '')
  check('/ opens the 主控台 and it is the first tab', firstTab.includes('主控台') && (await d.locator('[data-testid=tab-agent]').getAttribute('aria-selected')) === 'true', firstTab)
  check('ticker on the console: fixed 正常 / 異常 / 待核准 counts', /正常\s*\d+[\s\S]*異常\s*\d+[\s\S]*待核准\s*\d+/.test(await d.locator('[data-testid=status-strip]').innerText().catch(() => '')))
  const r = [(await api(d, '/api/sim/control', { action: 'scenario', scenario: 'press7_temp_drop' })).status,
    (await api(d, '/api/sim/control', { action: 'speed', speed: 30 })).status, (await api(d, '/api/sim/control', { action: 'play' })).status]
  check('press-7 scenario started', r.every((s) => s === 200), r.join(','))
  const side = d.locator('[data-testid=plan-area]')
  const gotCard = await until(d, async () => (await side.locator('[data-testid=approval-visual]').count()) > 0, 150)
  await api(d, '/api/sim/control', { action: 'speed', speed: 5 })      // keep the plant running while we talk
  check('approval card v1 reached the agent tab', gotCard)
  const v1 = (await api(d, '/api/approvals?status=pending')).json?.[0]
  check('v1 is version 1 and sends #A-311 to press 9', v1?.version === 1 && (v1?.actions ?? []).some((a) => a.includes('#A-311 改 9 號')), (v1?.actions ?? [])[0])

  // ---------------- (a) discuss the waiting plan
  check('input defaults to 討論這個計劃 while a plan waits', (await d.locator('[data-testid=mode-discuss]').getAttribute('aria-checked')) === 'true')
  await d.locator('[data-testid=chat-input]').fill('9 號機今晚要保養')
  await d.locator('[data-testid=chat-input]').press('Enter')
  const gotV2 = await until(d, async () => (await side.locator('[data-testid=card-version-tag]').innerText().catch(() => '')) === 'v2')
  check('card v2 replaced v1 in the side column', gotV2)
  const tr = d.locator('[data-testid=transcript]')
  await d.waitForTimeout(1500)
  const me = await tr.locator('[data-testid=chat-me]').last().innerText().catch(() => '')
  check('transcript shows the supervisor message', me.includes('9 號機今晚要保養'), me.slice(0, 60))
  const reply = await tr.locator('[data-testid=chat-reply]').last().innerText().catch(() => '')
  check('agent reply names v2 and the code check', /v2|第 2 版/.test(reply) && reply.includes('程式已檢查'), reply.replace(/\n/g, ' ').slice(0, 160))
  check('agent posted a plain summary for v2', (await tr.locator('[data-testid=chat-summary]', { hasText: '我建議' }).count()) >= 2)
  // issue #37: chips, the replan row and the card version are steps, shown on the Agent page (看完整過程)
  await d.locator('[data-testid=see-full]').click()
  await d.waitForSelector('[data-testid=trace-body] [data-kind=state]', { timeout: 10000 }).catch(() => {})
  {
    const tr = d.locator('[data-testid=trace-body]')
    const chips = await tr.locator('[data-testid=constraint-chips]').last().innerText().catch(() => '')
    check('transcript shows the constraint chip 「9 號機 今晚不可用」', chips.includes('9 號機 今晚不可用'), chips.replace(/\n/g, ' ').slice(0, 80))
    check('transcript shows a replan row', (await tr.locator('[data-kind=state][data-to=replan]').count()) >= 1)
    const ver = await tr.locator('[data-testid=card-version]').last().innerText().catch(() => '')
    check('transcript shows card v1 → v2 with what changed', ver.includes('v1 → v2') && /#A-311：9 號 →/.test(ver), ver.replace(/\n/g, ' | ').slice(0, 160))
  }
  await d.locator('[data-testid=tab-agent]').click()
  await d.waitForSelector('[data-testid=transcript]')
  const changes = await side.locator('[data-testid=card-changes]').innerText().catch(() => '')
  check('card v2 shows 「與 v1 相比」 and #A-311 off press 9', changes.includes('與 v1 相比') && /#A-311：9 號 →/.test(changes), changes.replace(/\n/g, ' | ').slice(0, 160))
  const pend = (await api(d, '/api/approvals?status=pending')).json ?? []
  const v2 = pend.find((x) => x.version === 2)
  check('API: one pending card, v2, supersedes v1', pend.length === 1 && v2?.supersedes === v1?.id, pend.map((x) => `${x.id}/v${x.version}`).join(','))
  check('API: v2 no longer sends #A-311 to press 9', !!v2 && !(v2.actions ?? []).some((a) => a.includes('#A-311 改 9 號')), (v2?.actions ?? [])[0])
  await d.screenshot({ path: `${OUT}/plan-talk-v2-1440.png` })

  // the phone shows the newest version (390 wide)
  const ph = await ctx.newPage()
  await ph.setViewportSize({ width: 390, height: 844 })
  await ph.goto(BASE + '/phone', { waitUntil: 'networkidle' })
  const phOk = await until(ph, async () => (await ph.locator('[data-testid=phone-version]').count()) > 0, 20)
  check('phone shows the newest card v2 and what changed', phOk && (await ph.locator('[data-testid=phone-changes]').innerText()).includes('#A-311'))
  await ph.screenshot({ path: `${OUT}/plan-talk-phone-390.png`, fullPage: true })
  await ph.close()
  await d.setViewportSize({ width: 390, height: 844 })
  await d.waitForTimeout(600)
  const fit = await d.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)
  check('agent tab fits 390 wide (no horizontal scroll)', fit)
  await d.screenshot({ path: `${OUT}/plan-talk-v2-390.png` })
  await d.setViewportSize({ width: 1440, height: 900 })
  await d.waitForTimeout(400)

  // v1 is retired; v2 approves
  const old = await api(d, `/api/approvals/${v1.id}/decision`, { request_id: v1.id, decision: 'approve', via: 'dashboard' })
  check('approving the superseded v1 is refused (409)', old.status === 409, `${old.status} ${old.json?.detail ?? ''}`)
  await side.locator('[data-testid=visual-approve]').first().click()
  const approved = await until(d, async () => {
    const all = (await api(d, '/api/approvals')).json ?? []
    return all.some((x) => x.id === v2?.id && x.status === 'approved')
  }, 30)
  check('v2 approved from the card', approved)

  // ---------------- (b) a new task in plain Chinese
  await d.locator('[data-testid=mode-command]').click()
  await d.locator('[data-testid=chat-input]').fill('通知維修組明早檢查 3 號機')
  await d.locator('[data-testid=chat-input]').press('Enter')
  let cmd = null
  await until(d, async () => {
    const evs = (await api(d, '/api/events')).json ?? []
    cmd = evs.find((e) => e.source === 'supervisor' && e.command === '通知維修組明早檢查 3 號機')
    return !!cmd
  }, 30)
  check('下新任務 opened a supervisor event', !!cmd, cmd?.id)
  const cmdCard = await until(d, async () => ((await api(d, '/api/approvals?status=pending')).json ?? []).some((x) => x.event_id === cmd?.id))
  check('the task reached the gate and asks for approval (R2/R3)', cmdCard)
  const cc = ((await api(d, '/api/approvals?status=pending')).json ?? []).find((x) => x.event_id === cmd?.id)
  check('card is R3 (free-text notice needs the supervisor) and names the command', cc?.risk === 'R3' && (cc?.reason ?? '').includes('主管指令'), `${cc?.risk} ${cc?.reason}`)
  await d.waitForTimeout(1200)
  const cmdMe = await tr.locator('[data-testid=chat-me]', { hasText: '通知維修組明早檢查 3 號機' }).count()
  check('transcript switched to the task and shows it', cmdMe >= 1)
  await d.screenshot({ path: `${OUT}/plan-talk-command-1440.png` })
  const res = await api(d, `/api/approvals/${cc.id}/decision`, { request_id: cc.id, decision: 'approve', via: 'dashboard' })
  check('task card approved', res.status === 200, `${res.status}`)
  const closed = await until(d, async () => ((await api(d, `/api/events/${cmd.id}`)).json ?? {}).state === 'closed', 60)
  const evDone = (await api(d, `/api/events/${cmd.id}`)).json
  check('task event closed with the notice sent', closed && (evDone?.outcome ?? '').includes('已通知維修'), evDone?.outcome)
  const audit = (await api(d, `/api/audit?event_id=${encodeURIComponent(cmd.id)}&limit=500`)).json ?? []
  const kinds = new Set(audit.map((e) => e.kind))
  check('task audit: command, plan, gate, approval, notify, close', audit.some((e) => e.payload?.command) && kinds.has('llm_call') && kinds.has('gate')
    && kinds.has('approval_request') && kinds.has('approval_decision') && audit.some((e) => e.actor === 'tool:notify_teams')
    && audit.some((e) => e.kind === 'state_transition' && e.payload?.to === 'closed'), [...kinds].join(','))
  await d.waitForTimeout(800)
  await d.screenshot({ path: `${OUT}/plan-talk-command-done-1440.png` })
  await d.locator('[data-testid=status-strip]').click()
  await d.waitForTimeout(500)
  check('clicking the status strip opens 產線監控 (#monitor)', (await d.evaluate(() => location.hash)) === '#monitor' && (await d.locator('.tabs [role=tab][aria-selected=true]').innerText()).includes('產線監控'))
  check('no page errors', errors.length === 0, errors.join(' | ').slice(0, 200))
} finally {
  await b.close()
}
const failed = results.filter((x) => !x).length
console.log(`\n${results.length - failed}/${results.length} passed`)
process.exit(failed ? 1 : 0)
