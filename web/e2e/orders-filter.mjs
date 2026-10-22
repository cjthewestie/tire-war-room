// issues #171 / #172: the 訂單 tab -- customer counts follow the main tab and due day, switching the tab resets the customer,
// one chip per active filter (each removable), a clear empty-result line, and the 急單 / 有風險 explanations.
//   (start the app: ./start.sh)   node e2e/orders-filter.mjs [baseUrl]
import { createRequire } from 'node:module'
import { homedir } from 'node:os'

const require = createRequire(import.meta.url)
function loadPlaywright() {
  for (const id of ['playwright', 'playwright-core', `${homedir()}/.local/lib/node_modules/@playwright/cli/node_modules/playwright-core`]) {
    try { return require(id) } catch { /* next */ }
  }
  throw new Error('playwright not found: pnpm add -D playwright, or install @playwright/cli')
}
const { chromium } = loadPlaywright()
const BASE = process.argv[2] ?? 'http://localhost:8000'

let failed = 0
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }

const browser = await chromium.launch()
for (const [label, width, height] of [['1440', 1440, 1000], ['390', 390, 900]]) {
  const ctx = await browser.newContext({ viewport: { width, height }, locale: 'zh-TW', timezoneId: 'Asia/Taipei' })
  const p = await ctx.newPage()
  await p.goto(`${BASE}/#orders`, { waitUntil: 'networkidle' })
  await p.waitForSelector('[data-testid="orders-tab"]', { timeout: 20000 })
  await p.waitForTimeout(600)
  const tab = (name) => p.locator('.order-tools .seg button', { hasText: name }).first()
  const rows = () => p.locator('[data-testid="order-list"] li').count()
  const opts = () => p.locator('[data-testid="order-customer"] option').allInnerTexts()
  const parse = (t) => { const m = t.match(/（(\d+) 張）$/); return m ? Number(m[1]) : t.endsWith('（0 張／無符合訂單）') ? 0 : null }

  // 1. every customer's number = the rows you get by picking it (no search text), on every main tab
  for (const name of ['全部', '有風險', '急單', '進行中', '已完成', '已出貨']) {
    await tab(name).click()
    const texts = (await opts()).slice(1)
    let ok = true; let bad = ''
    for (let i = 0; i < texts.length; i++) {
      await p.locator('[data-testid="order-customer"]').selectOption({ index: i + 1 })
      const n = parse(texts[i]); const r = await rows()
      if (n !== r) { ok = false; bad = `${texts[i]} → ${r} rows` }
    }
    check(`[${label}] ${name}: customer numbers = list rows`, ok, bad)
    await p.locator('[data-testid="order-customer"]').selectOption({ index: 0 })
  }

  // 2. a customer with nothing under the tab stays in the menu as 「0 張／無符合訂單」
  await tab('有風險').click()
  check(`[${label}] 有風險 tab keeps customers with 0 orders, marked`, (await opts()).some((t) => t.endsWith('（0 張／無符合訂單）')))

  // 3. search text and the picked customer do not change the numbers
  const before = (await opts()).join('|')
  await p.fill('[data-testid="order-search"]', 'zzz-no-such')
  check(`[${label}] search text does not change customer numbers`, (await opts()).join('|') === before)
  await p.fill('[data-testid="order-search"]', '')
  await p.locator('[data-testid="order-customer"]').selectOption({ index: 1 })
  check(`[${label}] picked customer does not change customer numbers`, (await opts()).join('|') === before)

  // 4. the main-tab numbers stay the totals
  const segsBefore = await p.locator('.order-tools .seg button').allInnerTexts()
  await tab('全部').click(); await p.locator('[data-testid="order-customer"]').selectOption({ index: 1 }); await p.fill('[data-testid="order-search"]', 'a')
  check(`[${label}] main-tab numbers stay the totals`, (await p.locator('.order-tools .seg button').allInnerTexts()).join('|') === segsBefore.join('|'))
  await p.fill('[data-testid="order-search"]', '')

  // 5. switching the main tab resets the customer
  await p.locator('[data-testid="order-customer"]').selectOption({ index: 1 })
  await tab('進行中').click()
  check(`[${label}] switching the tab resets the customer to 所有客戶`, (await p.locator('[data-testid="order-customer"]').inputValue()) === '')

  // 6. chips: one per active filter, each removable; the empty line names no order number
  await tab('已出貨').click()
  await p.locator('[data-testid="order-customer"]').selectOption({ index: 1 })
  await p.fill('[data-testid="order-search"]', 'zzz-no-such')
  const chips = p.locator('[data-testid="order-filters"] .order-chip')
  check(`[${label}] three chips (分頁 / 客戶 / 搜尋)`, (await chips.count()) === 3, (await chips.allInnerTexts()).join(' | '))
  const empty = await p.locator('[data-testid="order-empty"]').innerText()
  check(`[${label}] empty line says the conditions have no match`, empty.includes('目前條件沒有符合的訂單') && !empty.includes('請確認訂單號'), empty)
  await p.locator('.order-chip', { hasText: '搜尋' }).click()
  check(`[${label}] taking off the search chip clears the search box`, (await p.inputValue('[data-testid="order-search"]')) === '' && (await chips.count()) === 2)
  await p.locator('.order-chip', { hasText: '客戶' }).click()
  check(`[${label}] taking off the customer chip resets the menu`, (await p.locator('[data-testid="order-customer"]').inputValue()) === '' && (await chips.count()) === 1)
  await p.locator('.order-chip', { hasText: '分頁' }).click()
  check(`[${label}] no filters → no chip row`, (await p.locator('[data-testid="order-filters"]').count()) === 0)

  // 7. 清除所有條件 still works
  await tab('急單').click(); await p.fill('[data-testid="order-search"]', 'zzz-no-such')
  await p.getByRole('button', { name: '清除所有條件' }).click()
  check(`[${label}] 清除所有條件 restores the full list`, (await p.locator('[data-testid="order-filters"]').count()) === 0 && (await rows()) > 0)

  // 8. the day chip (from the 未來 7 日交貨計畫) keeps its test id and can be taken off
  const day = p.locator('.dw-li').first()
  if (await day.count()) {
    await day.click()
    const dayChip = p.locator('[data-testid="due-filter"]')
    check(`[${label}] picking a due day shows a day chip`, (await dayChip.count()) === 1, (await dayChip.allInnerTexts()).join(''))
    if (await dayChip.count()) { await dayChip.click(); check(`[${label}] taking off the day chip`, (await p.locator('[data-testid="due-filter"]').count()) === 0) }
  }

  // 9. the two KPI explanations, and no sideways scroll
  const hints = await p.locator('.kpi-hint').allInnerTexts()
  check(`[${label}] 急單 / 有風險 explanations`, hints.includes('客戶要求優先處理') && hints.includes('依目前排程，可能無法準時交貨'), hints.join(' | '))
  check(`[${label}] no horizontal scroll`, await p.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1))
  await ctx.close()
}
await browser.close()
console.log(failed ? `\n${failed} FAILED` : '\nall passed')
process.exit(failed ? 1 : 0)
