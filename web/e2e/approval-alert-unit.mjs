// Unit checks (no server, no browser) for the 待核准 bar on every page (PendingApprovalAlert + approvalAlert.ts;
// CJ, 2026-10-22: 「核准區有代辦時應該全部頁面都要看得到告警」): it renders with N > 0 and is gone at 0, the countdown and
// the red / pulse thresholds (R3, < 2 min), the link goes to the oldest card, and the tab-title prefix.
// Run: cd web && node e2e/approval-alert-unit.mjs     (vite loads the TS/TSX modules directly)
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const here = dirname(fileURLToPath(import.meta.url))
let failed = 0
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }

globalThis.location ??= { search: '', hash: '', pathname: '/', protocol: 'http:', host: 'localhost' }   // transport.ts reads it at load
const vite = await createServer({ root: resolve(here, '..'), configFile: false, logLevel: 'error', appType: 'custom',
  server: { middlewareMode: true, hmr: false, ws: false }, esbuild: { jsx: 'automatic' } })
const load = (p) => vite.ssrLoadModule(p)
const { alertModel, oldestPending, fmtClock, fmtWaited, titleWithPending, approveHref, URGENT_S } = await load('/src/approvalAlert.ts')
const { PendingApprovalAlert } = await load('/src/components/PendingApprovalAlert.tsx')
const { WarRoomCtx } = await load('/src/warRoomCtx.ts')
const { initialState } = await load('/src/store.ts')
const React = await import('react')
const { renderToStaticMarkup } = await import('react-dom/server')

// factory time; at 10× the 10 min wall timeout is 100 min of factory time (state_machine._approval_expires_at)
const T0 = Date.parse('2026-10-22T08:00:00+08:00')
const iso = (t) => new Date(t).toISOString()
const card = (id, ev, createdMin, expiresMin, risk = 'R2', title = `卡 ${id}`) => ({
  id, event_id: ev, step_id: null, round: 1, created_at: iso(T0 + createdMin * 60e3), risk, title, reason: '', done: [], gaps: [],
  delivery_impact: '', options: [], status: 'pending', decision: null, expires_at: expiresMin == null ? null : iso(T0 + expiresMin * 60e3) })

// ---------------------------------------------------------------- the oldest card, the count
{
  const p = [card('b', 'EV-B', 5, 15), card('a', 'EV-A', 2, 12), card('c', 'EV-C', 8, 18)]
  check('oldest = created first, whatever the list order', oldestPending(p)?.id === 'a', oldestPending(p)?.id)
  const m = alertModel(p, T0 + 4 * 60e3, 1)
  check('count = 3', m.n === 3, String(m.n))
  check('link targets the oldest event', approveHref(m.oldest.event_id) === '/#approve?event=EV-A', approveHref(m.oldest.event_id))
  check('link keeps ?mockch / ?live and encodes the id', approveHref('EV 1/x', '?live=1') === '/?live=1#approve?event=EV%201%2Fx', approveHref('EV 1/x', '?live=1'))
  check('nothing pending → no model', alertModel([], T0, 1) === null)
}

// ---------------------------------------------------------------- countdown and thresholds (wall seconds)
{
  const p = [card('a', 'EV-A', 0, 10)]
  const at = (min, speed = 1) => alertModel(p, T0 + min * 60e3, speed)
  check('2 min in: 已等 2 分, 8:00 left, amber', at(2).leftS === 480 && fmtWaited(at(2).waitedS) === '已等 2 分' && !at(2).red && !at(2).pulse, JSON.stringify(at(2)))
  check('fmtClock 135 → 2:15, 0 → 0:00', fmtClock(135) === '2:15' && fmtClock(0) === '0:00' && fmtClock(-3) === '0:00')
  check('fmtWaited under a minute in seconds', fmtWaited(45.7) === '已等 45 秒', fmtWaited(45.7))
  check(`exactly ${URGENT_S} s left: still amber`, !at(8).red && !at(8).pulse, String(at(8).leftS))
  check('119 s left: red and pulsing', at(8 + 1 / 60).red && at(8 + 1 / 60).pulse, String(at(8 + 1 / 60).leftS))
  check('expired: 0 s left, red', at(11).leftS === 0 && at(11).red)
  // 10× sim: expires_at is created_at + 100 factory min; 20 factory min in = 2 wall min waited, 8 left
  const q = [card('a', 'EV-A', 0, 100)]
  const m10 = alertModel(q, T0 + 20 * 60e3, 10)
  check('10× speed: factory gaps ÷ speed = wall time', m10.leftS === 480 && Math.round(m10.waitedS) === 120, `${m10.leftS} / ${m10.waitedS}`)
  check('no expires_at: no countdown, amber', alertModel([card('a', 'EV-A', 0, null)], T0, 1).leftS === null)
  const r3 = alertModel([card('a', 'EV-A', 0, 10), card('z', 'EV-Z', 1, 11, 'R3')], T0 + 60e3, 1)
  check('an R3 card waiting: red, no pulse', r3.red && !r3.pulse && r3.oldest.id === 'a')
  check('no clock yet: still shown, times from 0', alertModel(p, NaN, 1)?.waitedS === 0)
}

// ---------------------------------------------------------------- tab title
check('title prefix while N > 0', titleWithPending('實心胎智慧製造戰情室', 3) === '(3) 待核准・實心胎智慧製造戰情室')
check('no prefix at 0', titleWithPending('交班報告', 0) === '交班報告')

// ---------------------------------------------------------------- the component (server render)
{
  const render = (pending, props = {}, clock = { factory_time: iso(T0 + 3 * 60e3), speed: 1, paused: false }) =>
    renderToStaticMarkup(React.createElement(WarRoomCtx.Provider, { value: { state: { ...initialState, pending, clock }, transport: null, connected: true } },
      React.createElement(PendingApprovalAlert, props)))
  const p = [card('b', 'EV-B', 2, 12, 'R2', '換 7 號機模具'), card('a', 'EV-A', 1, 11, 'R2', 'B 線開煉機停機檢修')]
  const html = render(p)
  check('renders with N > 0', html.includes('data-testid="pending-alert"') && html.includes('待核准 2 件'), html.slice(0, 160))
  check('… the oldest title, waited and countdown', html.includes('B 線開煉機停機檢修') && html.includes('已等 2 分') && html.includes('8:00 後逾時'))
  check('… a link to the oldest card on 主控台', html.includes('href="/#approve?event=EV-A"') && html.includes('data-event="EV-A"'))
  check('… 去核准 →', html.includes('去核准 →'))
  check('new tab when asked (report / handover / factory)', render(p, { newTab: true }).includes('target="_blank"'))
  check('in-app: a button, no link', render(p, { onGo: () => {} }).startsWith('<div') && !render(p, { onGo: () => {} }).includes('href='))
  check('amber normally', html.includes('data-level="amber"') && !html.includes(' pulse'))
  check('red + pulse under 2 min', /data-level="red"/.test(render(p, {}, { factory_time: iso(T0 + 9.5 * 60e3), speed: 1, paused: false })) &&
    render(p, {}, { factory_time: iso(T0 + 9.5 * 60e3), speed: 1, paused: false }).includes('pulse'))
  check('hidden at 0', render([]) === '', render([]))
  check('variants carry their class', render(p, { variant: 'corner' }).includes('paa-wrap-corner') && render(p, { variant: 'compact' }).includes('paa-wrap-compact'))
}

await vite.close()
console.log(failed ? `\n${failed} FAILED` : '\nall passed')
process.exit(failed ? 1 : 0)
