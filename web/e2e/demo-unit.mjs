// Unit checks (no server, no browser) for the 3D factory's 6-minute guided demo timeline (factory3d/demoCues.ts):
// cue order, the 3:00 pitch cue on time whatever the plant does, event-driven advance with the time as a fallback,
// skip (N) and the pausable demo clock.
// Run: cd web && node e2e/demo-unit.mjs
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const here = dirname(fileURLToPath(import.meta.url))
let failed = 0
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }

const vite = await createServer({ root: resolve(here, '..'), configFile: false, logLevel: 'error', appType: 'custom',
  server: { middlewareMode: true, hmr: false, ws: false } })
const D = await vite.ssrLoadModule('/src/factory3d/demoCues.ts')
await vite.close()

/** Run the timeline second by second; sig(t) gives the live signals at t. Returns the fired cues. */
function run(sig, until = D.DEMO_TOTAL) {
  let tl = D.startTimeline()
  for (let t = 0; t <= until; t += 0.5) tl = D.step(tl, t, sig(t))
  return tl
}
const at = (tl, id) => tl.fired.find((f) => f.id === id)

// order
const ids = D.CUES.map((c) => c.id)
check('cue order', ids.join(',') === 'start,console,event,phone,worker,agent,pitch,warn,end', ids.join(','))
check('planned times rise', D.CUES.every((c, i) => i === 0 || c.at > D.CUES[i - 1].at))
check('total is 6:00', D.DEMO_TOTAL === 360 && D.CUES.at(-1).at === 360)
check('pitch cue at 3:00, full screen', D.CUES.find((c) => c.id === 'pitch').at === 180 && D.CUES.find((c) => c.id === 'pitch').big)
check('1-minute warning at 5:00', D.CUES.find((c) => c.id === 'warn').at === 300)
check('one scenario constant', D.DEMO_SCENARIO === 'morning_rush' && D.MAIN_LOADS[0].scenario === D.DEMO_SCENARIO)

// nothing happens on the plant: every cue fires on its time fallback, in order, pitch exactly at 180
{
  const tl = run(() => ({}))
  check('no events: every cue fires', tl.fired.length === D.CUES.length, tl.fired.map((f) => f.id).join(','))
  check('no events: all by time', tl.fired.slice(1).every((f) => f.reason === 'time'))
  check('no events: event cue waits its grace (100 + 15)', at(tl, 'event').t === 115, String(at(tl, 'event').t))
  check('no events: pitch at 3:00', at(tl, 'pitch').t === 180, String(at(tl, 'pitch').t))
  check('no events: warn at 5:00, end at 6:00', at(tl, 'warn').t === 300 && at(tl, 'end').t === 360)
}

// the two measured runs (morning_rush 40×, the real model): cues follow the events, each up ≥ 10 s, pitch at 3:00
const latched = (f) => { let l = {}; return (t) => (l = D.latch(l, f(t))) }
for (const [name, ev, card, crew, closed, want] of [
  ['run A', 84.6, 101.9, 108.6, 111.4, { event: 84.6, phone: 101.9, worker: 111.9, agent: 121.9 }],
  ['run B', 103.4, 135.1, 142.4, 145.1, { event: 103.4, phone: 135.1, worker: 145.1, agent: 155.1 }],
]) {
  // the card is pending only until the presenter approves it 6 s later: the latch keeps the signal
  const tl = run(latched((t) => ({ event: t >= ev && t < closed, approval: t >= card && t < card + 6, worker: t >= crew && t < closed, closed: t >= closed })), 400)
  const got = Object.fromEntries(['event', 'phone', 'worker', 'agent'].map((id) => [id, at(tl, id).t]))
  const near = (a, b) => Math.abs(a - b) < 0.51
  check(`${name}: cues on the events`, Object.keys(want).every((k) => near(got[k], want[k]) && at(tl, k).reason === 'event'), JSON.stringify(got))
  check(`${name}: pitch at 3:00`, at(tl, 'pitch').t === 180)
}

// without the latch an approved card would be missed (the reason latch() exists)
{
  const tl = run((t) => ({ event: t >= 100, approval: t >= 102 && t < 108 }))
  check('unlatched: approved card inside the dwell is missed → fallback', at(tl, 'phone').reason === 'time' && at(tl, 'phone').t === 145, JSON.stringify(at(tl, 'phone')))
}

// a very slow model: no card by 2:25 → the phone cue shows anyway on its fallback (130 + 15)
{
  const tl = run((t) => ({ event: t >= 90, approval: t >= 170 }))
  check('very slow: phone cue by time at 145', at(tl, 'phone').t === 145 && at(tl, 'phone').reason === 'time', JSON.stringify(at(tl, 'phone')))
  check('very slow: pitch still at 3:00', at(tl, 'pitch').t === 180)
}

// signals already true at the start don't fire event cues before their minAt
{
  const tl = run(() => ({ event: true, approval: true, worker: true, closed: true }), 55)
  check('stale signals: no event cue before minAt 50', at(tl, 'event')?.t === 50, JSON.stringify(at(tl, 'event')))
  check('stale signals: one cue per step', tl.fired.map((f) => f.id).join(',') === 'start,console,event', tl.fired.map((f) => f.id).join(','))
}

// N (skip) and the clock jump into the pitch
{
  let tl = D.startTimeline()
  for (let i = 0; i < 6; i++) tl = D.skip(tl, 20)
  check('skip x6 → pitch', D.current(tl).id === 'pitch' && tl.fired.at(-1).reason === 'skip')
  check('skip into pitch jumps the clock to 3:00', D.skipTime(tl, 20) === 180)
  check('skip before the pitch keeps the clock', D.skipTime(D.skip(D.startTimeline(), 5), 5) === 5)
  let end = tl
  for (let i = 0; i < 10; i++) end = D.skip(end, 200)
  check('skip stops at the end', D.current(end).id === 'end' && D.done(end))
  check('upcoming after start is 主控台', D.upcoming(D.startTimeline()).id === 'console')
}

// pausable clock
{
  let c = D.clockStart(1000)
  check('clock reads seconds', D.clockRead(c, 4000) === 3)
  c = D.clockPause(c, 4000)
  check('paused clock holds', D.clockRead(c, 99000) === 3)
  c = D.clockResume(c, 99000)
  check('resumed clock continues', D.clockRead(c, 101000) === 5)
  c = D.clockSet(c, 180, 101000)
  check('clock set (skip to pitch)', D.clockRead(c, 102000) === 181)
  check('mss', D.mss(185) === '3:05' && D.mss(0) === '0:00' && D.mss(-3) === '0:00')
}

console.log(failed ? `\n${failed} FAILED` : '\nall passed')
process.exit(failed ? 1 : 0)
