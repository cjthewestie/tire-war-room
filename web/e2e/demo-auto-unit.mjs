// Unit checks (no server, no browser) for the auto-play demo (/factory?demo=cure07; ?mock=1 fallback): CJ's nine beats
// in order (demo/beat.ts: overview → walk → orders → leave → anomaly → phone → repair → robot → back), each driven by its
// cue in the auto timeline (demo/cues.ts advance(…, auto) with demo/auto.ts AUTO_STEPS), the run ending at the war
// room (no pitch), the auto-approve only in the mock (never on the real backend), and a whole run of the mock engine
// (急單 → 7 號機) through the real driver (demo/driver.ts DemoRun) with the 3D's walk faked by a clock.
// Run: cd web && node e2e/demo-auto-unit.mjs
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const here = dirname(fileURLToPath(import.meta.url))
let failed = 0
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }

// the page's URL, as the mock fallback is opened (auto.ts and mock/engine.ts read it when called)
const setUrl = (search) => { globalThis.location = { search } }
setUrl('?mock=1&scenario=press7_temp_drop&demo=cure07')

const vite = await createServer({ root: resolve(here, '..'), configFile: false, logLevel: 'error', appType: 'custom',
  server: { middlewareMode: true, hmr: false, ws: false } })
const C = await vite.ssrLoadModule('/src/demo/cues.ts')
const AU = await vite.ssrLoadModule('/src/demo/auto.ts')
const BT = await vite.ssrLoadModule('/src/demo/beat.ts')
const DR = await vite.ssrLoadModule('/src/demo/driver.ts')
const E = await vite.ssrLoadModule('/src/mock/engine.ts')
const ST = await vite.ssrLoadModule('/src/store.ts')
await vite.close()

// ---- the contract ----
check('beat ids (fixed contract)', BT.DEMO_BEATS.join(',') === 'overview,walk,orders,leave,anomaly,phone,repair,robot,back', BT.DEMO_BEATS.join(','))
check('auto cues', AU.AUTO_CUES.join(',') === 'start,rush,event,h1,worker,robot,agent', AU.AUTO_CUES.join(','))
check('every auto cue is a real cue, in CUES order', AU.AUTO_CUES.every((id, i, a) => C.CUES.findIndex((c) => c.id === id) >= 0
  && (i === 0 || C.CUES.findIndex((c) => c.id === id) > C.CUES.findIndex((c) => c.id === a[i - 1]))))
check('the run ends at the war room (agent), never the pitch', AU.AUTO_FINAL === 'agent' && !AU.AUTO_CUES.includes('pitch'))
for (const b of BT.DEMO_BEATS) check(`beat ${b} has a cue and a caption`, AU.AUTO_CUES.includes(BT.BEAT_CUE[b]) && !!AU.AUTO_TEXT[b])
check('beatOf: rush is walk until the desk, then orders, then leave', BT.beatOf('rush', false) === 'walk' && BT.beatOf('rush', true) === 'orders' && BT.beatOf('rush', true, true) === 'leave')
check('beatOf: outside the auto cues → null', BT.beatOf('pitch') === null && BT.beatOf(null) === null && BT.beatOf('verify') === null)
check("#194's timings kept", AU.AUTO.openS === 4.5 && AU.AUTO.analysisS === 18 && AU.AUTO.speed === 10 && AU.AUTO.phoneAfterS === 1.5)

// the store: set / get / subscribe, null clears
let calls = 0
const off = BT.subscribeDemoBeat(() => calls++)
BT.setDemoBeat('walk'); BT.setDemoBeat('walk'); BT.setDemoBeat('orders'); BT.setDemoBeat(null)
off()
BT.setDemoBeat('robot')
check('beat store notifies on change only, null clears', calls === 3 && BT.getDemoBeat() === 'robot', `calls ${calls}`)
BT.setDemoBeat(null)

// ---- the URL switches ----
check('?mock=1&demo=cure07: auto, mock approves', AU.autoRequested() && AU.autoMockRequested() && AU.autoActive('mock') && AU.autoApproves('mock'))
setUrl('?demo=cure07')
check('/factory?demo=cure07: auto on the real backend', AU.autoRequested() && !AU.autoMockRequested() && AU.autoActive('live'))
check('real backend never auto-approves', !AU.autoApproves('live'))
const liveRun = new DR.DemoRun({ mode: 'live', control: async () => {} }, '3d', true)
check('DemoRun on the real backend: auto, autoApprove off', liveRun.auto && !liveRun.autoApprove)
setUrl('?demo=1')
check('other demo values are not the auto demo', !AU.autoRequested() && !AU.autoActive('live'))
setUrl('?mock=1&scenario=press7_temp_drop&demo=cure07')

// ---- the pure timeline with scripted signals (a fake plant, the 3D's walk = 2.5 s after each cue) ----
function play(plant, { until = 400, walkS = 2.5 } = {}) {
  let tl = C.startTimeline()
  const beats = []
  let sig = {}
  for (let t = 0; t <= until; t = Math.round((t + 0.1) * 10) / 10) {
    const c = C.CUES[tl.index]
    const walking = c.shot.kind !== 'establish'
    const arrived = walking ? t - tl.enteredAt >= walkS : undefined
    sig = C.latch(sig, plant(t))
    const r = C.advance(tl, t, arrived === undefined ? sig : { ...sig, arrived }, false, true)
    tl = r.tl
    const cc = C.CUES[tl.index]
    const atDesk = cc.id === 'rush' && t - tl.enteredAt >= walkS
    const b = BT.beatOf(cc.id, atDesk, tl.released)
    if (beats[beats.length - 1]?.beat !== b) beats.push({ beat: b, t })
  }
  return { tl, beats }
}
// the plant: the rush card at 11 s, approved (by whoever) at 15, done at 18; 7 號機 at 21, its card at 22.5, approved 41, repaired 48
const at = (from) => (t) => t >= from
const plant = (o) => (t) => ({
  rushCard: at(o.rushCard)(t), rushApproved: at(o.rushOk)(t), rushDone: at(o.rushDone)(t),
  event: at(o.event)(t), approval: at(o.card)(t) && t < o.ok, approved: at(o.ok)(t), repaired: at(o.fixed)(t),
})
const P = { rushCard: 11, rushOk: 15, rushDone: 18, event: 21, card: 22.5, ok: 41, fixed: 48 }
const { tl, beats } = play(plant(P))
const seq = beats.map((b) => b.beat).join(',')
check('nine beats in CJ order', seq === BT.DEMO_BEATS.join(','), seq)
const tOf = (b) => beats.find((x) => x.beat === b)?.t
check('overview from 0', tOf('overview') === 0)
const H = (id) => AU.AUTO_STEPS[id].hold
const near = (x, want, slack = 0.3) => x != null && x >= want - 0.05 && x < want + slack
// pacing (CJ: 「每一個步驟不可以太趕，要順暢」): every beat at least its prompter line (≈ 4 字/秒, ≥ 7 s)
for (const b of BT.DEMO_BEATS) check(`beat ${b} min ≥ 7 s`, AU.beatMinS(b) >= 7, `${AU.beatMinS(b)}`)
check('walk after the opening line', near(tOf('walk'), H('start').min), tOf('walk'))
check('orders once at the desk', near(tOf('orders'), tOf('walk') + 2.5), tOf('orders'))
check('leave once the order is done and has been seen', near(tOf('leave'), Math.max(tOf('walk') + H('rush').min, P.rushDone + AU.RESULT_S.orders)), tOf('leave'))
check('anomaly after the walk out', near(tOf('anomaly'), Math.max(P.event, tOf('leave') + AU.AUTO_STEPS.event.enter.min)), tOf('anomaly'))
check('phone on the 7 號機 card, after the anomaly look', near(tOf('phone'), Math.max(P.card, tOf('anomaly') + H('event').min)), String(tOf('phone')))
check('repair after the approval has been seen on the phone', near(tOf('repair'), Math.max(P.ok + AU.RESULT_S.phone, tOf('phone') + H('h1').min)), tOf('repair'))
check('robot after the press is green and held', near(tOf('robot'), Math.max(P.fixed + AU.RESULT_S.repair, tOf('repair') + H('worker').min)), tOf('robot'))
check('back after the robot show', near(tOf('back'), tOf('robot') + H('robot').min), `${tOf('back')}`)
console.log('      paced run:', beats.map((b) => `${b.beat}@${b.t}`).join('  '))
check('the run stays at the war room (no pitch, nothing after)', C.CUES[tl.index].id === 'agent' && !tl.released)

// the anomaly that comes early (while the order is still being resolved) waits for the orders beat to finish
{
  const r = play(plant({ ...P, event: 12, card: 13.5 }))
  const a = r.beats.find((x) => x.beat === 'anomaly')?.t
  check('early anomaly: orders still finish first', a >= P.rushDone && r.beats.map((b) => b.beat).join(',') === BT.DEMO_BEATS.join(','), `${a}`)
}
// the real backend: nobody has tapped yet → the approve beat waits (never skipped while the card is up)
{
  const r = play(plant({ ...P, ok: 1e9, fixed: 1e9 }), { until: 300 })
  const last = r.beats[r.beats.length - 1]
  check('no tap: the phone beat holds for the tap', last.beat === 'phone', last.beat)
}
// a scenario without a rush card (the live fallback press7_temp_drop): orders moves on after 40 s, the rest plays
{
  const r = play(plant({ ...P, rushCard: 1e9, rushOk: 1e9, rushDone: 1e9, event: 60, card: 61.5, ok: 80, fixed: 87 }))
  const o = r.beats.find((x) => x.beat === 'anomaly')?.t
  check('no rush card: on to the anomaly once it comes', r.beats.map((b) => b.beat).join(',') === BT.DEMO_BEATS.join(',') && o >= 60, `${o}`)
}
// the robot beat waits for the walk / show (arrived) but never longer than its timeout
{
  const r = play(plant(P), { walkS: 1e9, until: 900 })
  const w = r.beats.find((x) => x.beat === 'back')?.t
  check('a walk that never arrives still ends (timeouts)', !!w, `${w}`)
}

// ---- the mock engine through the real driver (?mock=1&scenario=press7_temp_drop&demo=cure07) ----
{
  const eng = new E.MockEngine({ scenario: 'press7_temp_drop' })
  let state = ST.initialState
  eng.subscribe((m) => { state = ST.reduce(state, { kind: 'ws', msg: m }) })
  const decided = []
  const io = {
    mode: 'mock',
    control: async (c) => { eng.control(c) },
    decide: async (d) => { decided.push({ id: d.request_id, via: d.via, at: run.t, cue: C.CUES[run.tl.index].id }); return eng.decide(d) },
  }
  const run = new DR.DemoRun(io, '3d', true)
  check('mock DemoRun auto-approves', run.auto && run.autoApprove)
  const t0 = performance.now()
  await run.start(t0)
  const beats = []
  let firstRushCard = null, firstEvent = null
  for (let ms = 0; ms <= 200_000; ms += 100) {
    if (!eng.paused) eng.now += 100 * eng.speed
    eng['publish'](false)
    await Promise.resolve()
    const now = t0 + ms
    const c = C.CUES[run.tl.index]
    run.arrived = c.shot.kind === 'establish' ? undefined : (now - (run._entered ?? 0)) >= 2500
    const before = run.tl.index
    run.tick(now, state)
    if (run.tl.index !== before) run._entered = now
    const cc = C.CUES[run.tl.index]
    const b = BT.beatOf(cc.id, cc.id === 'rush' && now - (run._entered ?? 0) >= 2500, run.tl.released)
    if (beats[beats.length - 1]?.beat !== b) beats.push({ beat: b, t: Math.round(run.t * 10) / 10 })
    if (firstRushCard == null && state.pending.some((p) => p.event_id.endsWith('-RUSH'))) firstRushCard = run.t
    if (firstEvent == null && (state.snapshot?.events ?? []).some((e) => e.machine_id === 'CURE-07')) firstEvent = run.t
  }
  const seq2 = beats.map((b) => b.beat).join(',')
  console.log('      mock run:', beats.map((b) => `${b.beat}@${b.t}`).join('  '))
  check('mock run: nine beats in order', seq2 === BT.DEMO_BEATS.join(','), seq2)
  check('mock run: rush card comes (~11 s)', firstRushCard != null && firstRushCard > 7 && firstRushCard < 14, `${firstRushCard}`)
  check('mock run: 7 號機 comes (~21 s)', firstEvent != null && firstEvent > 18 && firstEvent < 25, `${firstEvent}`)
  check('mock run: two auto approvals (rush in orders, 7 號機 in phone)', decided.length === 2
    && decided[0].id === 'APR-RUSH-1' && decided[0].cue === 'rush' && decided[1].id === 'APR-P7-1' && decided[1].cue === 'h1', JSON.stringify(decided))
  check('mock run: 7 號機 approved after the 18 s look', decided[1] && decided[1].at - firstEvent >= AU.AUTO.analysisS, `${decided[1]?.at}`)
  const evs = state.snapshot?.events ?? []
  check('mock run: the rush order and 7 號機 both closed', evs.length === 2 && evs.every((e) => e.state === 'closed'), evs.map((e) => `${e.id}:${e.state}`).join(' '))
  check('mock run: 7 號機 back to green', state.snapshot?.machines.find((m) => m.machine_id === 'CURE-07')?.status === 'running')
  const wr = beats.find((b) => b.beat === 'back')?.t
  check('mock run: back at the war room within ~3 min', wr != null && wr < 180, `${wr}`)
  check('mock run: ends at the war room', C.CUES[run.tl.index].id === 'agent' && decided.length === 2)
}

console.log(failed ? `\n${failed} FAILED` : '\nall passed')
process.exit(failed ? 1 : 0)
