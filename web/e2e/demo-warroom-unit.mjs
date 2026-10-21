// Unit checks (no server, no browser) for the war room's demo mode (src/demo/beats.ts, src/demo/signals.ts): every
// shared cue has a beat, each beat opens the right page with the right anchor, the focused event fills the anchor,
// the approval beat waits for the approval, and the worker signal needs a real approval first.
// Run: cd web && node e2e/demo-warroom-unit.mjs
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const here = dirname(fileURLToPath(import.meta.url))
let failed = 0
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }

const vite = await createServer({ root: resolve(here, '..'), configFile: false, logLevel: 'error', appType: 'custom',
  server: { middlewareMode: true, hmr: false, ws: false } })
const B = await vite.ssrLoadModule('/src/demo/beats.ts')
const C = await vite.ssrLoadModule('/src/demo/cues.ts')
const S = await vite.ssrLoadModule('/src/demo/signals.ts')
const G = await vite.ssrLoadModule('/src/demo/gamepad.ts')
await vite.close()

// every shared cue has a war room beat
for (const c of C.CUES) check(`beat for cue ${c.id}`, !!B.BEATS[c.id])

// beat → tab + first anchor; the caption is the cue's title (one wording per beat, cues.ts)
const want = {
  start: ['agent', 'home-overview'], console: ['agent', 'home-item-{id}'], event: ['agent', 'plan-area'],
  worker: ['monitor', 'data-machine'], agent: ['console', 'hyp-rejected'], robot: ['sm', 'sm-stages'],
  rush: ['agent', 'approve-bar'], h1: ['agent', 'approve-bar'], verify: ['agent', 'result-strip'], h2: ['agent', 'approve-bar'],
}
for (const [id, [tab, anchor]] of Object.entries(want)) {
  const b = B.beatFor(id)
  check(`${id} → ${tab}`, b.tab === tab, b.tab)
  check(`${id} anchor ${anchor}`, b.anchors[0].includes(anchor), b.anchors[0])
  check(`${id} caption = the 3D title`, b.caption === C.CUES.find((c) => c.id === id).title, b.caption)
}
check('worker wording agreed for both screens', B.beatFor('worker').caption.includes('員工完成維修，程式驗證讀值'))
for (const id of ['pitch', 'warn', 'end']) check(`${id} is the calm pitch card`, B.BEATS[id].pitch === true && B.BEATS[id].anchors.length === 0)
check('unknown cue → caption only on 主控台', B.beatFor('nope').tab === 'agent' && B.beatFor('nope').anchors.length === 0)
check('no cue → no beat', B.beatFor(null) === null)

// the focused event fills the anchors
const ev = (id, machine, state, opened) => ({ id, machine_id: machine, state, opened_at: opened })
const events = [ev('EV-PLAN', 'PLANT', 'closed', '2026-10-23T07:00:00+08:00'), ev('EV-ORD', 'PLANT', 'await_approval', '2026-10-23T07:21:00+08:00'), ev('EV-7', 'CURE-07', 'plan', '2026-10-23T07:40:00+08:00'),
  ev('EV-OLD', 'CURE-03', 'closed', '2026-10-23T07:01:00+08:00')]
check('incident = newest open machine event', B.focusEvent('incident', events)?.id === 'EV-7')
check('order = open plant-wide event', B.focusEvent('order', events)?.id === 'EV-ORD')
check('no incident yet → no focus (not the day plan)', B.focusEvent('incident', events.filter((e) => e.machine_id === 'PLANT')) === null)
check('no focus → null', B.focusEvent(null, events) === null)
const v = B.beatView('console', { snapshot: { events } })
check('console beat anchors the order item', v.anchors[0] === '[data-testid="home-item-EV-ORD"]' && v.eventId === 'EV-ORD', v.anchors[0])
const v2 = B.beatView('console', { snapshot: { events: [] } })
check('no event yet → the {id} anchor is dropped, list next', v2.anchors[0] === '[data-testid="approval-list"]', v2.anchors[0])
const before = B.beatView('event', { snapshot: { events: events.filter((e) => e.machine_id === 'PLANT') } })
check('event beat before the incident: no order plan in the spotlight, the status strip', before.anchors[0] === '[data-testid="status-strip"]' && before.eventId === null, before.anchors.join(' | '))
check('event beat with the incident: its plan', B.beatView('event', { snapshot: { events } }).anchors[0] === '[data-testid="plan-area"]')
check('phone beat with the incident: the 核准 bar', B.beatView('phone', { snapshot: { events } }).anchors[0] === '[data-demo-anchor="approve-bar"]')
check('agent beat: H1 rejected → verdict → H2 first', B.BEATS.agent.anchors[0].startsWith('[data-testid="think-block"][data-hyp-status="rejected"] & [data-demo-anchor="hyp-rejected"] & '))
check('agent beat falls back to the trace and the task log', B.BEATS.agent.anchors.includes('[data-testid="trace-body"] & [data-testid="tasklog"]'))
check('agent beat opens the incident', B.beatView('agent', { snapshot: { events } }).eventId === 'EV-7')

// what each beat waits for
check('phone waits for the approval', B.waitingFor('phone', 'worker') === 'approval')
check('event-led next cue waits for the event', B.waitingFor('event', 'approval') === 'event')
check('a timed next cue waits for nothing', B.waitingFor('start', undefined) === null)

// signals: the worker beat needs an approval granted after the demo started
const ok = (e) => ({ status: 'approved', event_id: e })
const base = { snapshot: { events: [ev('EV-7', 'CURE-07', 'execute', '2026-10-23T07:40:00+08:00'), ev('EV-R', 'PLANT', 'await_approval', '2026-10-23T07:20:00+08:00')] },
  pending: [{ id: 'AP-R', event_id: 'EV-R' }], resolved: [ok('EV-OLD')], world: null }
const seen = S.freshSeen()
check('a rush-order card is not the incident approval', S.signals(base, seen).approval === false)
check('the incident card is', S.signals({ ...base, pending: [{ id: 'AP-7', event_id: 'EV-7' }] }, seen).approval === true)
check('worker not yet: the approval was before the demo', S.signals(base, seen).worker === false)
check('worker not on an order approval', S.signals({ ...base, resolved: [ok('EV-OLD'), ok('EV-R')] }, seen).worker === false)
check('worker after the incident approval', S.signals({ ...base, resolved: [ok('EV-OLD'), ok('EV-R'), ok('EV-7')] }, seen).worker === true)
const tiles = (repaired) => ({ machines: [{ machine_id: 'CURE-07', status: 'out_of_service', repaired }] })
check('workerDone not before the repair (停用, not green)', S.signals({ ...base, snapshot: { ...base.snapshot, ...tiles(false) } }, seen).workerDone === false)
const afterH2 = { ...base, resolved: [ok('EV-OLD'), { id: 'AP-7-1', ...ok('EV-7') }, { id: 'AP-7-2', ...ok('EV-7') }] }
check('workerDone needs this incident approved', S.signals({ ...base, snapshot: { ...base.snapshot, ...tiles(true) } }, seen).workerDone === false)
check('workerDone on machines[].repaired (green 檢修完成・待恢復運轉)', S.signals({ ...afterH2, snapshot: { ...base.snapshot, ...tiles(true) } }, seen).workerDone === true)
check('worker beat spotlights the 7 號機 tile', B.beatView('worker', { snapshot: { events } }).anchors[0] === '[data-machine="CURE-07"]')
check('incident followed', seen.id === 'EV-7' && seen.machine === 'CURE-07')

// the Xbox pad: A continues, Menu pauses, a held button fires once
const pad = (...down) => ({ buttons: Array.from({ length: 17 }, (_, i) => ({ pressed: down.includes(i) })) })
let e = G.padEdges(new Set(), [pad(G.PAD_A)])
check('A press → continue', e.pressed.includes(0) && G.PAD_A === 0)
e = G.padEdges(e.held, [pad(G.PAD_A)])
check('A held → no repeat', e.pressed.length === 0)
e = G.padEdges(e.held, [pad()])
e = G.padEdges(e.held, [null, pad(G.PAD_MENU)])
check('Menu (9) on a second pad → pause', e.pressed.join() === '9' && G.PAD_MENU === 9)

console.log(failed ? `\n${failed} FAILED` : '\nall passed')
process.exit(failed ? 1 : 0)
