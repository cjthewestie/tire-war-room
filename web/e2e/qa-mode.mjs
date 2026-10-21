// QA 模式 (#qa) against a live server with the real model: one chip at 1440 (normal + 大字模式) and at 390, plus the
// evidence viewer. Headless only.
//   PORT=8200 LLM_MODE=llm ... uv run python -m warroom.api serve   then   node e2e/qa-mode.mjs [baseUrl] [outDir]
import { createRequire } from 'node:module'
import { mkdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { resolve } from 'node:path'

const require = createRequire(import.meta.url)
function loadPlaywright() {
  for (const id of ['playwright', 'playwright-core', `${homedir()}/.local/lib/node_modules/@playwright/cli/node_modules/playwright-core`]) {
    try { return require(id) } catch { /* next */ }
  }
  throw new Error('playwright not found: pnpm add -D playwright, or install @playwright/cli')
}
const { chromium } = loadPlaywright()
const BASE = process.argv[2] ?? 'http://127.0.0.1:8200'
const OUT = resolve(process.argv[3] ?? 'e2e/screenshots')
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch({ headless: true })
const fail = (m) => { console.error('FAIL', m); process.exitCode = 1 }

async function run(width, height, name, { big = false, chip = 'AI 會不會亂改產線', viewer = false } = {}) {
  const page = await browser.newPage({ viewport: { width, height } })
  const errors = []
  page.on('pageerror', (e) => errors.push(String(e)))
  await page.goto(`${BASE}/#qa`)
  await page.getByTestId('qa-page').waitFor({ timeout: 15000 })
  if ((await page.getByTestId('qa-chip').count()) !== 12) fail('expected 12 chips')
  if (big) await page.getByTestId('qa-big-toggle').click()
  const t0 = Date.now()
  await page.getByTestId('qa-chip').filter({ hasText: chip }).click()
  await page.getByTestId('qa-answer').waitFor({ timeout: 120000 })
  const answer = await page.getByTestId('qa-answer').innerText()
  console.log(`${name}: ${((Date.now() - t0) / 1000).toFixed(1)} s\n${answer}\n`)
  if (!(await page.getByTestId('qa-evidence').count())) fail(`${name}: no evidence list`)
  const sw = await page.evaluate(() => document.documentElement.scrollWidth)
  if (sw > width) fail(`${name}: horizontal scroll ${sw} > ${width}`)
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: true })
  if (viewer) {
    await page.locator('.qa-ref').first().click()
    await page.getByTestId('qa-viewer').locator('.qa-code').waitFor({ timeout: 10000 })
    await page.screenshot({ path: `${OUT}/${name}-viewer.png` })
  }
  if (errors.length) fail(`${name}: page errors ${errors.join('; ')}`)
  await page.close()
}

await run(1440, 900, 'qa-1440', { viewer: true })
await run(1440, 900, 'qa-1440-big', { big: true, chip: '為什麼不用 NAT 做狀態機' })
await run(390, 844, 'qa-390', { chip: '斷網還能跑嗎' })
await browser.close()
console.log(`screenshots in ${OUT}`)
