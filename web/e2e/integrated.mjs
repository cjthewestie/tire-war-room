// Integrated run against the real backend (every module real, mock commander unless the server runs LLM_MODE=llm):
//   REPAIR_FAST_S=0 uv run python -m warroom.api serve --strict --port 8770   then   node web/e2e/integrated.mjs http://localhost:8770
// REPAIR_FAST_S=0: page 11's round 2 (7 號機停用) only comes when the round-1 fix does not hold; fast repair fixes it.
// Page 11: resets to press7_temp_drop, approves round 1 on the phone (system pick), picks A 夜班加班 in round 2.
// Page 2: open_mill_overheat, checks 已自動 / 待核准 on the dashboard, approves 放慢生料投入 from the dashboard.
// Screenshots -> docs/screenshots/integrated-*.png
import { createRequire } from 'node:module'
import { homedir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
let pw
for (const id of ['playwright', 'playwright-core', `${homedir()}/.local/lib/node_modules/@playwright/cli/node_modules/playwright-core`]) { try { pw = require(id); break } catch { /* next */ } }
const BASE = process.argv[2] ?? 'http://localhost:8000'
const SPEED = Number(process.argv[3] ?? 10)
const OUT = resolve(dirname(fileURLToPath(import.meta.url)), '../../docs/screenshots')
const results = []
const check = (n, ok, d = '') => { results.push(ok); console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${d ? `  — ${d}` : ''}`) }
const errors = []
const api = (path, body) => fetch(BASE + path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}).then((r) => r.json())
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
async function until(fn, secs, label) {
  for (let i = 0; i < secs * 2; i++) { const v = await fn(); if (v) return v; await sleep(500) }
  console.log(`  (timeout waiting for ${label})`); return null
}
const pendingOf = async () => (await api('/api/approvals?status=pending'))

const comp = await api('/api/health/components')
check('backend has no degraded components', Object.keys(comp.degraded ?? {}).length === 0, JSON.stringify(comp.degraded))

const b = await pw.chromium.launch()
const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, timezoneId: 'Asia/Taipei' })
const d = await ctx.newPage()
d.on('pageerror', (e) => errors.push(e.message))
const ph = await ctx.newPage()
ph.on('pageerror', (e) => errors.push(e.message))
await ph.setViewportSize({ width: 390, height: 844 })

// ---------------- page 11
await api('/api/sim/control', { action: 'scenario', scenario: 'press7_temp_drop' })
await api('/api/sim/control', { action: 'speed', speed: SPEED })
await d.goto(BASE + '/#monitor', { waitUntil: 'networkidle' })
await ph.goto(BASE + '/phone', { waitUntil: 'networkidle' })
await d.waitForSelector('.mode', { timeout: 10000 })
const mode = await d.locator('.mode').innerText()
check('dashboard is on the live backend (not mock)', /連線中|模型未連線/.test(mode) && !(await d.locator('.mode').getAttribute('class')).includes('mock'), mode)

const r1 = await until(async () => (await pendingOf())[0], 120, 'round-1 approval')
check('round 1 approval request (page 11 02:14)', !!r1, r1 ? `${r1.title} | ${r1.options.map((o) => o.id + ' ' + o.label).join(' / ')}` : '')
await d.waitForTimeout(1500)
const dashTxt = await d.locator('body').innerText()
check('focus panel shows press 7 event live', /7 號/.test(dashTxt) && dashTxt.includes('待核准'), dashTxt.match(/異常：[^\n]*/)?.[0] ?? '')
const hdr = await api('/api/snapshot')
// #100 took the counts out of the header; 產線監控 (#102) says them: 異常機台（N） and 其他 N 台正常
check('machine counts from backend', dashTxt.includes(`異常機台（${hdr.header.abnormal_count}）`) && dashTxt.includes(`其他 ${hdr.header.normal_count} 台正常`), `normal ${hdr.header.normal_count} / abnormal ${hdr.header.abnormal_count}`)
await d.screenshot({ path: `${OUT}/integrated-p11-dashboard-0214.png`, fullPage: true })
await ph.waitForSelector('.ph-opt', { timeout: 15000 })
const card1 = await ph.locator('.ph-card').first().innerText()
check('phone card round 1 with A/B', card1.includes('夜班加班') && card1.includes('從 2 線調 1 人'), card1.slice(0, 160).replace(/\n/g, ' | '))
await ph.screenshot({ path: `${OUT}/integrated-p11-phone-round1.png`, fullPage: true })
const rec = r1.options.find((o) => o.recommended) ?? r1.options[r1.options.length - 1]
await ph.click(`.ph-opt:has-text("${rec.label}")`)
await ph.click('.ph-approve')
const v2 = await until(async () => (await api('/api/schedule')).version >= 2, 30, 'schedule v2')
check('approval from phone activated schedule v2', !!v2)

await d.click('text=人力排程')
await d.waitForTimeout(2000)
const sched = await d.locator('body').innerText()
check('schedule tab: the approved version is in force', /現在用第 [2-9] 版排程/.test(sched), sched.match(/現在用.*/)?.[0] ?? '')
await d.screenshot({ path: `${OUT}/integrated-p11-schedule-v2.png`, fullPage: true })
await d.click('text=產線監控')

const r2 = await until(async () => (await pendingOf()).find((x) => x.round >= 2 || x.title.includes('停用')), 240, 'round-2 approval')
check('round 2 approval "7 號機停用，重排中"', !!r2 && r2.title === '7 號機停用，重排中', r2?.title ?? '')
await ph.waitForTimeout(1500)
await ph.waitForSelector('.ph-opt', { timeout: 15000 })
// the card says what happened in one sentence; why 判斷一 failed (溫度續降至 119.5°C) is in its folded 看依據 (d/ui-phone)
await ph.$eval('.ph-approval details.ph-why', (el) => { el.open = true }).catch(() => {})
const card2 = await ph.locator('.ph-card').first().innerText()
check('phone card round 2 shows H1 rejection reason', card2.includes('H1') || card2.includes('119'), card2.slice(0, 200).replace(/\n/g, ' | '))
await ph.screenshot({ path: `${OUT}/integrated-p11-phone-round2.png`, fullPage: true })
await d.waitForTimeout(1000)
await d.screenshot({ path: `${OUT}/integrated-p11-dashboard-0231.png`, fullPage: true })
await ph.click('.ph-opt:has-text("夜班加班")')
await ph.click('.ph-approve')
const closed = await until(async () => { const e = (await api('/api/events')).find((x) => x.machine_id === 'CURE-07'); return e && e.state === 'closed' ? e : null }, 60, 'event closed')
const tl = closed ? closed.timeline.map((t) => t.text).join(' | ') : ''
check('event closed: H2, press 7 out of service, overtime chosen', !!closed && tl.includes('H2') && tl.includes('夜班加班') && tl.includes('7 號機停用'), closed?.outcome ?? '')
check('supervisor rejection of cross-team move recorded', tl.includes('否決') || tl.includes('改選'), tl.match(/\[人工\][^|]*/g)?.slice(-1)[0] ?? '')
const m7 = await api('/api/machines/CURE-07')
check('CURE-07 out of service', m7.state.status === 'out_of_service', m7.state.status)
const c2 = await api('/api/health/components')
check('audit hash chain intact', c2.audit_chain_ok === true)
await d.click('text=人力排程')
await d.waitForTimeout(2500)
await d.screenshot({ path: `${OUT}/integrated-p11-schedule-final.png`, fullPage: true })
await d.click('text=產線監控')
await ph.waitForTimeout(1000)
await ph.screenshot({ path: `${OUT}/integrated-p11-phone-after.png`, fullPage: true })

// ---------------- page 2
await api('/api/sim/control', { action: 'scenario', scenario: 'open_mill_overheat' })
const p2 = await until(async () => (await pendingOf())[0], 120, 'page-2 approval')
check('page 2 pending approval (R3 放慢生料投入)', !!p2 && p2.risk === 'R3', p2?.title ?? '')
await d.waitForTimeout(2000)
// what was done sits in the focus panel's folded 處理紀錄 (UX declutter): open it before reading
await d.$eval('[data-testid=focus-timeline]', (el) => { el.open = true }).catch(() => {})
await d.waitForTimeout(300)
const t2 = await d.locator('body').innerText()
check('dashboard: 已自動 通知維修 + 待核准 放慢生料投入', t2.includes('已自動') && t2.includes('通知維修') && t2.includes('放慢生料投入'), '')
check('dashboard: schedule impact text', t2.includes('大胎 #07 仍 17:00 入硫化機'))
await d.screenshot({ path: `${OUT}/integrated-p2-dashboard.png`, fullPage: true })
const big = d.locator('text=大開煉').first()
if (await big.count()) { await big.click(); await d.waitForTimeout(1500); await d.screenshot({ path: `${OUT}/integrated-p2-machine-drawer.png`, fullPage: true }); await d.keyboard.press('Escape') }
await d.goto(BASE + '/#monitor', { waitUntil: 'networkidle' })          // close the drawer
// issue #102: 產線監控 shows the approval as one line; 到主控台核准 opens its card on 主控台, where 核准 is
await d.waitForSelector('[data-testid=monitor-pending-go]', { timeout: 15000 }).catch(() => {})
await d.locator('[data-testid=monitor-pending-go]').click().catch(() => {})
await d.waitForSelector('[data-testid=plan-area] [data-testid=visual-approve]', { timeout: 15000 }).catch(() => {})
const btn = d.locator('[data-testid=plan-area] [data-testid=visual-approve]').first()
check('dashboard 核准 button present (主控台 card, from 到主控台核准)', (await btn.count()) > 0)
if (await btn.count()) { await btn.click() }
const done2 = await until(async () => { const e = (await api('/api/events')).find((x) => x.machine_id === 'A-BIGOPEN'); return e && ['closed', 'safe_stop'].includes(e.state) ? e : null }, 240, 'page-2 close')
check('page 2 event closed after approval', done2?.state === 'closed', done2?.outcome ?? '')
await d.waitForTimeout(1500)
await d.screenshot({ path: `${OUT}/integrated-p2-dashboard-after.png`, fullPage: true })

check('no page errors', errors.length === 0, errors.slice(0, 3).join(' || '))
await b.close()
console.log(`\n${results.filter(Boolean).length}/${results.length} integrated checks passed`)
process.exit(results.every(Boolean) ? 0 : 1)
