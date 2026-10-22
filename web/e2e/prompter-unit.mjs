// Unit checks (no browser) for the teleprompter text (src/demo/script.ts): every beat has a line, the next-line
// and progress logic. Run: cd web && node e2e/prompter-unit.mjs
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const here = dirname(fileURLToPath(import.meta.url))
let failed = 0
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }
const vite = await createServer({ root: resolve(here, '..'), configFile: false, logLevel: 'error', appType: 'custom',
  server: { middlewareMode: true, hmr: false, ws: false } })
const S = await vite.ssrLoadModule('/src/demo/script.ts')
await vite.close()

const IDS = ['overview', 'walk', 'orders', 'anomaly', 'approve', 'repair', 'robot', 'warroom']
check('beat order is the fixed contract', S.BEATS.join() === IDS.join(), S.BEATS.join())
check('every beat has a non-empty line', IDS.every((b) => typeof S.SCRIPT[b] === 'string' && S.SCRIPT[b].trim().length > 0))
check('no extra lines', Object.keys(S.SCRIPT).length === IDS.length)
check('wording: overview / anomaly / warroom', S.SCRIPT.overview.startsWith('大家好，我們是新創團隊 Team Westie') &&
  S.SCRIPT.anomaly.includes('7 號硫化機溫度往下掉') && S.SCRIPT.warroom === '回到戰情室，接下來我直接操作給大家看。')
check('null beat → null', S.lineFor(null) === null && S.lineFor(undefined) === null)
check('unknown beat → null', S.lineFor('nope') === null)
IDS.forEach((b, i) => {
  const l = S.lineFor(b)
  const nextOk = i < IDS.length - 1 ? l.next === S.SCRIPT[IDS[i + 1]] : l.next === null
  check(`${b}: current, next, progress`, l.index === i && l.total === 8 && l.current === S.SCRIPT[b] && nextOk &&
    Math.abs(l.progress - (i + 1) / 8) < 1e-9, JSON.stringify({ i: l.index, p: l.progress }))
})
check('first progress 1/8, last full', S.lineFor('overview').progress === 0.125 && S.lineFor('warroom').progress === 1)

console.log(failed ? `\n${failed} FAILED` : '\nall passed')
process.exit(failed ? 1 : 0)
