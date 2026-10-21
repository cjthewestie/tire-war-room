// Group D demo walk, headless (phone, approval card, AI status line, 3D field panel, 狀態機 forecast):
//  p.11 (press7_temp_drop): round 1 approved B over the API; on the round-2 card 「7 號機停用」 the phone's 修復預估 shows
//  SOP-CP-01 and no round-1 tasks / 預計完成, 「怎麼選」 names the remedy; 產線監控's folded card picks A without 展開 and
//  核准（A） sends option A; after close the status line says 專人維修中 while 停用檢修 runs and the 3D panel on 7 號
//  shows the 停用檢修 task, not a checklist. concurrent_3: the phone's idle card lists one status line per open event and
//  狀態機 shows 「約 N 分後達上限 120°C」 for B 線開煉機 instead of the 229.7°C extrapolation.
// Run against a live server: PORT=8010 ./start.sh & node web/e2e/demo-d.mjs http://localhost:8010
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
const BASE = process.argv[2] ?? 'http://localhost:8000'
const OUT = resolve(dirname(fileURLToPath(import.meta.url)), 'screenshots')
mkdirSync(OUT, { recursive: true })
let failed = 0
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
async function until(fn, ms, step = 500) {
  const t0 = Date.now()
  while (Date.now() - t0 < ms) { const v = await fn(); if (v) return v; await sleep(step) }
  return null
}
const api = async (path, body) => {
  const r = await fetch(BASE + path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {})
  return r.json()
}
const ctl = (b) => api('/api/sim/control', b)
const text = (page, sel) => page.$eval(sel, (el) => el.innerText).catch(() => null)
const pendingCard = (round) => until(async () => (await api('/api/approvals?status=pending')).find((a) => a.round === round), 240000, 1000)

const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] })

// ---------------------------------------------------------------- p.11
await ctl({ action: 'scenario', scenario: 'press7_temp_drop', speed: 20, preroll: true })
const r1 = await pendingCard(1)
check('p.11 round-1 card', !!r1, r1?.title)
await api(`/api/approvals/${r1.id}/decision`, { request_id: r1.id, decision: 'approve', option_id: 'B', via: 'dashboard' })
const r2 = await pendingCard(2)
check('p.11 round-2 card', !!r2, r2?.title)
await ctl({ action: 'pause' })        // the card waits while the screens are read

const phone = await browser.newPage({ viewport: { width: 390, height: 844 } })
await phone.goto(`${BASE}/phone`)
await until(() => phone.$('[data-testid=phone-eta-sop]'), 30000)
const eta = await text(phone, '[data-testid=phone-eta]')
check('round-2 修復預估: SOP-CP-01 硫化中斷處置', /SOP-CP-01/.test(eta ?? ''), (eta ?? '').split('\n').find((l) => /SOP-/.test(l)))
check('round-2 修復預估: no round-1 tasks, no 預計完成', !(await phone.$('[data-testid=phone-eta-tasks]')) && !(await phone.$('[data-testid=phone-eta-done]')))
const guide = await text(phone, '[data-testid=option-guide]')
check('round-2 怎麼選: 解除異常靠 = 7 號機停用交維修', /解除異常靠：7 號機停用交維修，其他模次改由別台/.test(guide ?? ''), (guide ?? '').split('\n')[1])
check('round-2 怎麼選: why B', /系統建議 B：排程求解器挑的方案；交期相同，想降低風險可改選 A/.test(guide ?? ''))
const ai2 = await text(phone, '[data-testid=ai-status]')
check('round-2 status line: what = the detection, not 「7 號機停用，重排中」', /AI 偵測到：7 號 128 → 122°C/.test(ai2 ?? ''), (ai2 ?? '').replace(/\n/g, '｜'))
await phone.screenshot({ path: `${OUT}/demo-d-phone-390-round2.png`, fullPage: true })

const dash = await browser.newPage({ viewport: { width: 1440, height: 900 } })
await dash.goto(`${BASE}/#monitor`)
await until(() => dash.$('[data-testid=apv-fold-opt-A]'), 30000)
await dash.screenshot({ path: `${OUT}/demo-d-monitor-1440-folded-B.png` })
await dash.click('[data-testid=apv-fold-opt-A]')
const label = await text(dash, '[data-testid=visual-approve]')
check('folded card: A toggle → 核准（A）', label === '核准（A）', label)
check('folded card: grid stays folded', !(await dash.$eval('.apv-compact', (el) => el.classList.contains('is-open'))))
await dash.screenshot({ path: `${OUT}/demo-d-monitor-1440-folded-A.png` })
await dash.click('[data-testid=visual-approve]')
const decided = await until(async () => (await api('/api/approvals')).find((a) => a.id === r2.id && a.status !== 'pending'), 15000)
check('核准（A） sends option A on the same request', decided?.decision?.option_id === 'A', `${decided?.id} ${decided?.status} ${decided?.decision?.option_id}`)
await ctl({ action: 'play' })
const closed = await until(async () => (await api('/api/events')).find((e) => e.machine_id === 'CURE-07' && e.state === 'closed'), 60000, 1000)
check('p.11 event closed', !!closed, closed?.outcome)
await ctl({ action: 'pause' })
await phone.reload()
// the task board (GET /api/world) lands a moment after the page: wait for it before reading the phase
const idle = (await until(async () => { const t = await text(phone, '.ph-card.idle [data-testid=ai-status]'); return t && /專人維修中/.test(t) ? t : null }, 20000))
  ?? await text(phone, '.ph-card.idle [data-testid=ai-status]')
check('closed + 停用檢修 running → 專人維修中, not 已修復', /專人維修中（0\/1）/.test(idle ?? '') && !/已修復/.test(idle ?? ''), (idle ?? '').replace(/\n/g, '｜'))
const hist = await phone.$$eval('.ph-card.idle .ph-hist', (els) => els.map((e) => e.innerText))
check('history: no two identical lines', new Set(hist.map((h) => h.replace(/^\S+・/, ''))).size === hist.length, hist.join(' / '))
await phone.screenshot({ path: `${OUT}/demo-d-phone-390-closed.png`, fullPage: true })

const f3 = await browser.newPage({ viewport: { width: 1440, height: 900 } })
await f3.goto(`${BASE}/factory?demo=1&test=1`)
await f3.click('[data-testid=f3-start]', { force: true, timeout: 60000 })
await until(() => f3.evaluate(() => !!window.__factory3d), 30000)
await f3.evaluate(() => { document.exitPointerLock?.(); window.__factory3d.standAt('CURE-07') })
await until(() => f3.$('[data-testid=f3-prompt]'), 20000)
await f3.keyboard.press('KeyF')
const out = await until(() => text(f3, '[data-testid=field-sop-out]'), 20000)
check('3D 7 號 (停用): 停用檢修 status, no checklist', /停用檢修/.test(out ?? '') && !(await f3.$('[data-testid=field-sop-step-0]')), out)
await f3.screenshot({ path: `${OUT}/demo-d-factory-1440-out.png` })
await f3.close()
await ctl({ action: 'play' })

// ---------------------------------------------------------------- concurrent_3
await ctl({ action: 'scenario', scenario: 'concurrent_3', speed: 10, preroll: true })
const seen = new Set()
await until(async () => {
  for (const a of await api('/api/approvals?status=pending')) {
    if (seen.has(a.id)) continue
    seen.add(a.id)
    const rec = (a.options.find((o) => o.recommended) ?? a.options[0])?.id ?? null
    await api(`/api/approvals/${a.id}/decision`, { request_id: a.id, decision: 'approve', option_id: rec, via: 'dashboard' })
  }
  const evs = await api('/api/events')
  const open = evs.filter((e) => !['closed', 'safe_stop'].includes(e.state))
  return seen.size >= 3 && open.length >= 2 && !(await api('/api/approvals?status=pending')).length
}, 180000, 1000)
await ctl({ action: 'pause' })
await phone.reload()
await until(() => phone.$('.ph-card.idle [data-testid=ai-status]'), 20000)
const lines = await phone.$$eval('.ph-card.idle [data-testid=ai-status]', (els) => els.map((e) => e.innerText.split('\n')[0]))
check('phone idle card: one status line per open event', lines.length >= 2, lines.join(' / '))
check('phone idle card: one 修復預估 only', (await phone.$$('.ph-card.idle [data-testid=phone-eta]')).length <= 1)
await phone.screenshot({ path: `${OUT}/demo-d-phone-390-c3.png`, fullPage: true })
const sm = await browser.newPage({ viewport: { width: 1440, height: 900 } })
await sm.goto(`${BASE}/#sm`)
await until(() => sm.$('[data-testid=sm-anoms]'), 30000)
const row = await until(async () => (await sm.$$eval('[data-testid=sm-anoms] tr', (els) => els.map((e) => e.innerText))).find((t) => /開煉機/.test(t)), 20000)
check('狀態機: rising mill reads 約 N 分後達上限 120°C', /約 \d+ 分後達上限 120°C/.test(row ?? '') && !/229/.test(row ?? ''), (row ?? '').replace(/\s+/g, ' '))
await sm.screenshot({ path: `${OUT}/demo-d-sm-1440.png` })
await ctl({ action: 'play' })

await browser.close()
console.log(failed ? `${failed} FAILED` : 'ALL PASS')
process.exit(failed ? 1 : 0)
