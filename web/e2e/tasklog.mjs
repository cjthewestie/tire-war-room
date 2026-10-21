// 今天處理過的任務 survives 重來 and a server restart (CJ 2026-10-22: 「…不可以清掉」), headless.
// node web/e2e/tasklog.mjs <base> <out dir> <phase: before|after>
import { createRequire } from 'node:module'
import { homedir } from 'node:os'
const require = createRequire(import.meta.url)
const { chromium } = (() => { for (const id of ['playwright', 'playwright-core', `${homedir()}/.local/lib/node_modules/@playwright/cli/node_modules/playwright-core`]) { try { return require(id) } catch { /* next */ } } throw new Error('playwright not found') })()
const [BASE = 'http://localhost:8130', OUT = '.', PHASE = 'before', EID = ''] = process.argv.slice(2)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const api = async (p, b) => (await fetch(BASE + p, b ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) } : {})).json()
let eid = EID
const browser = await chromium.launch({ headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
if (PHASE === 'before') {
  const r = await api('/api/command', { text: '#A-311 可能晚交，請重排保交期' })
  eid = r.event_id
  for (let i = 0; i < 240; i++) {
    const p = (await api('/api/approvals?status=pending')).find((a) => a.event_id === eid)
    const ev = await api(`/api/events/${eid}`)
    if (p) { await api(`/api/approvals/${p.id}/decision`, { request_id: p.id, decision: 'approve' }) }
    if (['closed', 'safe_stop'].includes(ev.state)) break
    await sleep(1000)
  }
  await page.goto(`${BASE}/#console`)
  await page.locator(`[data-testid="tasklog-row"][data-event="${eid}"]`).first().waitFor({ timeout: 30000 })
  await page.screenshot({ path: `${OUT}/1-after-task.png`, fullPage: false })
  // ↺ 重來 left the dashboard with #100 (it is in the 3D 展示 menu): the same reset over the API
  await api('/api/sim/control', { action: 'reset' })
  await sleep(4000)
  await page.goto(`${BASE}/#console`)
  const kept = await page.locator(`[data-testid="tasklog-row"][data-event="${eid}"]`).first().waitFor({ timeout: 30000 }).then(() => true, () => false)
  await page.screenshot({ path: `${OUT}/2-after-reset.png`, fullPage: false })
  console.log(JSON.stringify({ eid, kept_after_reset: kept }))
} else {
  await page.goto(`${BASE}/#console`)
  const kept = await page.locator(`[data-testid="tasklog-row"][data-event="${eid}"]`).first().waitFor({ timeout: 30000 }).then(() => true, () => false)
  await page.screenshot({ path: `${OUT}/3-after-restart.png`, fullPage: false })
  console.log(JSON.stringify({ eid, kept_after_restart: kept }))
}
await browser.close()
