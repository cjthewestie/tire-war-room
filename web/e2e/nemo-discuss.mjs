// 討論 on a waiting plan, headless (CJ 2026-10-21: 「按了討論沒有用」): load p.11, open the waiting card on the 主控台,
// click 討論 → the caret is in the chat input bound to that plan; type a change → a new card version replaces it.
// Run against a live server: node web/e2e/nemo-discuss.mjs http://localhost:8130
import { createRequire } from 'node:module'
import { homedir } from 'node:os'

const require = createRequire(import.meta.url)
function loadPlaywright() {
  for (const id of ['playwright', 'playwright-core', `${homedir()}/.local/lib/node_modules/@playwright/cli/node_modules/playwright-core`]) {
    try { return require(id) } catch { /* next */ }
  }
  throw new Error('playwright not found')
}
const { chromium } = loadPlaywright()
const BASE = process.argv[2] ?? 'http://localhost:8130'
let failed = 0
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
async function until(fn, ms, step = 500) {
  const t0 = Date.now()
  while (Date.now() - t0 < ms) { const v = await fn(); if (v) return v; await sleep(step) }
  return null
}
const api = async (path, body) => (await fetch(BASE + path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {})).json()

await api('/api/sim/control', { action: 'scenario', scenario: 'press7_temp_drop', speed: 30 })
const card = await until(async () => (await api('/api/approvals?status=pending'))[0], 240000, 1000)
check('a waiting card exists', !!card, card?.id)
const browser = await chromium.launch({ headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
await page.goto(`${BASE}/#agent`)
await page.locator(`[data-testid="home-item-${card.event_id}"]`).click({ timeout: 30000 })
await page.locator('[data-testid="visual-discuss"]').click({ timeout: 30000 })
await sleep(400)
const focused = await page.evaluate(() => ({ id: document.activeElement?.getAttribute('data-testid'), plan: document.activeElement?.getAttribute('data-plan') }))
check('討論 puts the caret in the chat input bound to the plan', focused.id === 'chat-input' && focused.plan === 'waiting', JSON.stringify(focused))
const t0 = Date.now()
await page.locator('[data-testid="chat-input"]').fill('不要加班')
await page.keyboard.press('Enter')
const v2 = await until(async () => (await api('/api/approvals?status=pending')).find((a) => a.event_id === card.event_id && (a.version ?? 1) > (card.version ?? 1)), 180000, 1000)
check('a typed change makes a new card version', !!v2, v2 ? `v${v2.version} in ${((Date.now() - t0) / 1000).toFixed(1)} s` : 'none')
const shown = v2 && await until(async () => (await page.locator('[data-testid="plan-area"]').innerText()).includes(`v${v2.version}`) || null, 30000)
check('the card on screen updates to the new version', !!shown)
const reply = await page.locator('[data-testid="sc-agent"]').last().innerText().catch(() => '')
console.log('agent reply:', reply.slice(0, 160))
const btns = await page.locator('[data-testid="plan-area"] button').allInnerTexts()
check('the card offers only 核准 and 討論', btns.some((b) => b.startsWith('核准')) && btns.includes('討論') && !btns.some((b) => b.includes('派發')), JSON.stringify(btns))
await browser.close()
process.exit(failed ? 1 : 0)
