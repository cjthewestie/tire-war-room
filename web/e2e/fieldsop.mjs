// #72 現場處置 SOP, headless. 1440: fire 硫化機蒸汽壓不足 on a normal plant, walk to that press, F → SOP checklist →
// tick every step → 回報處置完成; approve over the API (standing in for the phone); the event must close and the press
// read normal again. 390: the 7 號機溫降 scenario (p.11), only that the checklist shows on a phone-width screen.
// (p.11 scripts a second fault, 持續洩壓, so a field fix there is undone on purpose: the story's 驗證失敗.)
// Run against a live server: PORT=8010 ./start.sh & node web/e2e/fieldsop.mjs http://localhost:8010
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

const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] })
for (const [w, h] of [[1440, 900], [390, 844]]) {
  let MID = 'CURE-07'
  if (w === 1440) {
    await api('/api/sim/control', { action: 'scenario', scenario: 'normal', speed: 10, preroll: false })
    MID = (await api('/api/sim/events/fire', { kind: 'steam_low' })).fired.ref
  } else await api('/api/sim/control', { action: 'scenario', scenario: 'press7_temp_drop', speed: 10, preroll: true })
  const page = await browser.newPage({ viewport: { width: w, height: h } })
  await page.goto(`${BASE}/factory?demo=1&test=1`)
  await page.click('[data-testid=f3-start]', { force: true, timeout: 60000 })
  await until(() => page.evaluate(() => !!window.__factory3d), 15000)
  const ev = await until(async () => (await api('/api/events')).find((e) => e.machine_id === MID && !['closed', 'safe_stop'].includes(e.state)), 60000, 1000)
  check(`${w}: ${MID} 有事件`, !!ev, ev?.state)
  await page.evaluate((id) => { document.exitPointerLock?.(); window.__factory3d.standAt(id) }, MID)
  await until(() => page.$('[data-testid=f3-prompt]'), 15000)
  await page.keyboard.press('KeyF')
  await until(() => page.$('[data-testid=field-sop-step-0]'), 30000)
  const sop = await page.$eval('[data-testid=field-sop]', (el) => el.innerText).catch(() => null)
  const n = await page.$$eval('[data-testid^=field-sop-step-]', (els) => els.length).catch(() => 0)
  check(`${w}: SOP 處置步驟出現`, !!sop && n >= 3, `${n} 步・${(sop ?? '').split('\n')[1] ?? ''}`)
  await page.screenshot({ path: `${OUT}/fieldsop-${w}-before.png` })
  if (w !== 1440) { await page.close(); continue }
  for (let i = 0; i < n; i++) await page.click(`[data-testid=field-sop-step-${i}]`)
  await page.click('[data-testid=field-sop-finish]')
  const ok = await until(() => page.$('[data-testid=field-sop-done]'), 10000)
  check('回報處置完成', !!ok)
  await page.screenshot({ path: `${OUT}/fieldsop-${w}-done.png` })
  // the phone's approval, over the API
  for (let r = 0; r < 4; r++) {
    const ap = (await api('/api/approvals?status=pending')).find((a) => a.event_id === ev.id)
    if (ap) await api(`/api/approvals/${ap.id}/decision`, { request_id: ap.id, decision: 'approve', via: 'phone' })
    const st = await until(async () => {
      const e = (await api('/api/events')).find((x) => x.id === ev.id)
      return e && ['closed', 'safe_stop'].includes(e.state) ? e : null
    }, 120000, 2000)
    if (st) { check('事件結案', st.state === 'closed', `${st.state}・${st.outcome}`); break }
    if (r === 3) check('事件結案', false, 'not closed after 4 approvals')
  }
  const tile = (await api(`/api/machines/${MID}`)).tiles?.[0]
  check(`${MID} 溫度回穩`, tile?.status === 'normal', `${tile?.value} ${tile?.status}`)
  await page.screenshot({ path: `${OUT}/fieldsop-${w}-after.png` })
  await page.close()
}
await browser.close()
console.log(failed ? `${failed} FAILED` : 'ALL PASS')
process.exit(failed ? 1 : 0)
