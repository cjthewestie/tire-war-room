// QA 模式 「評審疑問 → 展示」 against a live server with the real model: run all 10 proofs from the page, record the
// time until the result is visible, screenshot each result. Headless only.
//   node e2e/qa-proofs.mjs [baseUrl] [outDir]
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
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
mkdirSync(OUT, { recursive: true })
const browser = await chromium.launch({ headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
page.on('dialog', (d) => d.accept())
await page.goto(`${BASE}/#qa/proofs`)
await page.getByTestId('qa-proofs').waitFor()
const items = page.getByTestId('qa-proof')
await items.first().waitFor()
const n = await items.count()
const rows = []
for (let i = 0; i < n; i++) {
  const it = items.nth(i)
  const btn = it.getByTestId('qa-proof-run')
  const title = (await it.locator('b').first().innerText()).trim()
  if (await btn.isDisabled()) { rows.push({ title, status: 'pending (button disabled)' }); console.log(`- ${title}: pending`); continue }
  const t0 = Date.now()
  await btn.click()
  await it.getByTestId('qa-proof-result').waitFor({ timeout: 150000 })
  const s = (Date.now() - t0) / 1000
  const text = await it.getByTestId('qa-proof-result').innerText()
  await it.scrollIntoViewIfNeeded()
  await it.screenshot({ path: `${OUT}/proof-${String(i + 1).padStart(2, '0')}.png` })
  rows.push({ title, seconds: s, result: text })
  console.log(`\n### ${i + 1}. ${title}  (${s.toFixed(1)} s to visible)\n${text}`)
}
await page.screenshot({ path: `${OUT}/proofs-index.png`, fullPage: true })
writeFileSync(`${OUT}/proofs-live.json`, JSON.stringify(rows, null, 1))
await browser.close()
