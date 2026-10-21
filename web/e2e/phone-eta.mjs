// #73 修復預估 on /phone, headless: fire 硫化機蒸汽壓不足 on a normal plant; the approval card must say how long
// (verification window), by when, and how (SOP steps); after 核准 (over the API) the idle card lists the field tasks and their ETA.
// Run against a live server: PORT=8010 ./start.sh & node web/e2e/phone-eta.mjs http://localhost:8010
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
const text = (page, id) => page.$eval(`[data-testid=${id}]`, (el) => el.innerText).catch(() => null)

await api('/api/sim/control', { action: 'scenario', scenario: 'normal', speed: 10, preroll: false })
const fired = (await api('/api/sim/events/fire', { kind: 'steam_low' })).fired
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 390, height: 844 } })
await page.goto(`${BASE}/phone`)
const win = await until(() => text(page, 'phone-eta-window'), 90000, 1000)
check('核准卡：多久修好', !!win, win ?? '')
await until(() => page.$('[data-testid=phone-eta-sop]'), 60000)
const done = await text(page, 'phone-eta-done')
const sop = await text(page, 'phone-eta-sop')
check('核准卡：預計完成', !!done && /\d{2}:\d{2}/.test(done), done ?? '')
check('核准卡：怎麼做（SOP）', !!sop && /SOP-/.test(sop), (sop ?? '').split('\n')[0])
await page.screenshot({ path: `${OUT}/phone-eta-390-card.png`, fullPage: true })
// 核准 over the API (the button itself is the existing card; headless has no supervisor PIN prompt)
const ap = await until(async () => (await api('/api/approvals?status=pending'))[0], 60000, 1000)
await api(`/api/approvals/${ap.id}/decision`, { request_id: ap.id, decision: 'approve', via: 'phone' })
const tasks = await until(() => text(page, 'phone-eta-tasks'), 60000, 1000)
check('核准後：現場任務與預計時間', !!tasks, (tasks ?? '').replace(/\n/g, '／'))
await page.screenshot({ path: `${OUT}/phone-eta-390-after.png`, fullPage: true })
const wide = await browser.newPage({ viewport: { width: 1440, height: 900 } })
await wide.goto(`${BASE}/phone`)
await until(() => text(wide, 'phone-eta'), 15000)
await wide.screenshot({ path: `${OUT}/phone-eta-1440.png` })
console.log(`machine ${fired.ref}`)
await browser.close()
console.log(failed ? `${failed} FAILED` : 'ALL PASS')
process.exit(failed ? 1 : 0)
