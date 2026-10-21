// QA 模式 「看一次怎麼運作」 against a live server with the real model: pick an event, screenshot the tracker at each
// stage (event opened, planning, waiting for approval, after the approval, closed). The approval is decided through
// the normal API as the on-site seat (this stands in for CJ on the phone). Headless only.
//   node e2e/qa-walk.mjs [baseUrl] [outDir] [kind=p11] [speed=10]
import { createRequire } from 'node:module'
import { mkdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { resolve } from 'node:path'

const require = createRequire(import.meta.url)
function loadPlaywright() {
  for (const id of ['playwright', 'playwright-core', `${homedir()}/.local/lib/node_modules/@playwright/cli/node_modules/playwright-core`]) {
    try { return require(id) } catch { /* next */ }
  }
  throw new Error('playwright not found')
}
const { chromium } = loadPlaywright()
const BASE = process.argv[2] ?? 'http://127.0.0.1:8200'
const OUT = resolve(process.argv[3] ?? 'e2e/screenshots')
const KIND = process.argv[4] ?? 'p11'
const SPEED = Number(process.argv[5] ?? 10)
const LIMIT_S = Number(process.env.WALK_LIMIT_S ?? 900)
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch({ headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
page.on('dialog', (d) => d.accept())
const t0 = Date.now()
const sec = () => ((Date.now() - t0) / 1000).toFixed(1)
await page.goto(`${BASE}/#qa/walk`)
await page.getByTestId('qa-walk').waitFor()
await page.locator(`.qa-seg:has-text("${SPEED}×")`).first().click().catch(() => {})
await page.locator(`[data-testid="qa-walk-pick"][data-kind="${KIND}"]`).click()
await page.getByTestId('qa-tracker').waitFor({ timeout: 60000 })

const shot = async (name) => { await page.screenshot({ path: `${OUT}/walk-${KIND}-${name}.png`, fullPage: true }); console.log(`${sec()} s  ${name}`) }
const walkId = async () => (await (await fetch(`${BASE}/api/qa/proofs`)).json(), null)
await shot('0-started')

let seen = new Set(), approvals = 0, lastWalk = null
const wid = await page.evaluate(async () => null)
// find the walk id from the server: the newest walk is the one the page started
const latest = async () => {
  for (let i = 1; i < 50; i++) {
    const r = await fetch(`${BASE}/api/qa/walk/WALK-${String(i).padStart(3, '0')}`)
    if (!r.ok) return lastWalk
    lastWalk = await r.json()
  }
  return lastWalk
}
void walkId; void wid
while ((Date.now() - t0) / 1000 < LIMIT_S) {
  const w = await latest()
  if (w) {
    for (const s of w.stages_reached) if (!seen.has(s)) { seen.add(s); await shot(`s-${s}`) }
    if (w.waiting_approval.length) {
      await shot(`approval-${approvals + 1}`)
      for (const id of w.waiting_approval) {
        const card = await (await fetch(`${BASE}/api/approvals`)).json()
        const a = card.find((x) => x.id === id)
        const opt = a?.options?.find((o) => o.recommended)?.id ?? a?.options?.[0]?.id ?? null
        const r = await fetch(`${BASE}/api/approvals/${id}/decision`, { method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ request_id: id, decision: 'approve', option_id: opt, decided_by: 'qa-walk e2e (CJ stand-in)' }) })
        console.log(`${sec()} s  approve ${id}: ${r.status}`)
      }
      approvals++
      await fetch(`${BASE}/api/qa/walk/${w.id}/control`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ action: 'play' }) })
      await page.waitForTimeout(3000)
      await shot(`after-approval-${approvals}`)
    }
    if (w.closed) { await page.waitForTimeout(2500); await shot('closed'); break }
  }
  await page.waitForTimeout(2000)
}
const w = await latest()
console.log(JSON.stringify({ kind: KIND, seconds: Number(sec()), event: w?.event_id, closed: w?.closed,
  states: w?.states?.map((s) => `${s.state}${s.llm_calls ? `(AI×${s.llm_calls})` : ''} ${s.seconds}s`),
  tracker: w?.tracker?.map((t) => `${t.page}: ${t.checks.map((c) => `${c.done ? '✓' : '○'}${c.what}${c.at ? '@' + c.at.slice(11, 19) : ''}`).join(' | ')}`) }, null, 1))
await browser.close()
