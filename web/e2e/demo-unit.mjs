// Unit checks (no server, no browser) for the guided demo (src/demo/cues.ts): cue order, presenter checkpoints that block
// until continue with the sim paused meanwhile, the approval checkpoint that continue cannot skip, the worker beat on the
// real task completion, the robot beat (and its show order, factory3d/patrol.ts), the 3:00 soft target with the 3:30
// automatic pitch, and the pitch's 2:00 / 3:00 marks. Also the deck manifest parser (factory3d/pitchSlides.ts) and the
// warning-light severity (factory3d/alarm.ts).
// Run: cd web && node e2e/demo-unit.mjs
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const here = dirname(fileURLToPath(import.meta.url))
let failed = 0
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }

const vite = await createServer({ root: resolve(here, '..'), configFile: false, logLevel: 'error', appType: 'custom',
  server: { middlewareMode: true, hmr: false, ws: false } })
const D = await vite.ssrLoadModule('/src/demo/cues.ts')
const P = await vite.ssrLoadModule('/src/factory3d/patrol.ts')
const A = await vite.ssrLoadModule('/src/factory3d/alarm.ts')
const S = await vite.ssrLoadModule('/src/factory3d/pitchSlides.ts')
const SG = await vite.ssrLoadModule('/src/demo/signals.ts')
const DR = await vite.ssrLoadModule('/src/demo/driver.ts')
await vite.close()

const ids = D.CUES.map((c) => c.id)
check('cue order (H1 → H2, CJ 2026-10-22)', ids.join(',') === 'start,rush,console,event,h1,verify,h2,worker,robot,agent,pitch,warn,end', ids.join(','))
check('presenter checkpoints (the 急單 approval is extra)', D.CHECKPOINTS.join(',') === 'start,console,event,h1,verify,h2,worker,robot,agent,pitch', D.CHECKPOINTS.join(','))
check('every checkpoint is a continue or approval hold', D.CHECKPOINTS.every((id) => D.CUES.find((c) => c.id === id).hold.wait !== 'auto'))
check('one scenario constant', D.DEMO_SCENARIO === 'morning_rush' && D.MAIN_LOADS[0].scenario === D.DEMO_SCENARIO)

/** Drive the timeline: script = { t: { cont?: true, sig?: {...} } } merged as time passes; returns the trace. */
function drive(script, until, dt = 0.25) {
  let tl = D.startTimeline(), sig = {}
  const trace = []
  for (let t = 0; t <= until + 1e-9; t = Math.round((t + dt) * 100) / 100) {
    const s = script[t] ?? {}
    if (s.sig) sig = D.latch(sig, s.sig)
    const r = D.advance(tl, t, sig, !!s.cont)
    trace.push({ t, index: r.tl.index, cue: D.CUES[r.tl.index].id, released: r.tl.released, paused: D.simPaused(r.tl, t, sig), waiting: D.waiting(r.tl, t, sig), refused: !!r.refused })
    tl = r.tl
  }
  return { tl, trace, at: (t) => trace.find((x) => x.t === t) }
}

// ① a checkpoint blocks until continue; the sim is paused while it waits, and plays again after
{
  const r = drive({}, 60)
  check('① blocks: no continue → still on start at 60 s', r.tl.index === 0 && !r.tl.released)
  check('① camera / orders first (12 s): sim runs until then', r.at(11).paused === false && r.at(11).waiting === null)
  check('① then waits for continue with the sim paused', r.at(12).paused === true && r.at(12).waiting === 'continue')
  const c = drive({ 20: { cont: true } }, 22)
  check('continue releases ① and the next cue enters', c.at(20).released && c.at(20.25).cue === 'rush', JSON.stringify(c.at(20.25)))
  check('sim resumed after continue', c.at(20).paused === false && c.at(20.25).paused === false)
}

// the 3D's walk gate: a checkpoint is not ready (no 「按 A 繼續」, no sim pause) until the lead has arrived
{
  const tl = D.startTimeline()
  check('walking: not ready while arrived is false', !D.ready(tl, 20, { arrived: false }) && !D.simPaused(tl, 20, { arrived: false }))
  check('arrived (or no gate, the war room): ready', D.ready(tl, 20, { arrived: true }) && D.ready(tl, 20, {}))
  const r = D.advance(tl, 20, { arrived: false }, true)
  check('walking: continue waits for the arrival', r.refused && !r.tl.released)
}

// rush: an approval checkpoint — continue is refused while the card is up, the approval moves on, sim keeps running
{
  const r = drive({ 1: { cont: true }, 3: { sig: { rushCard: true } }, 5: { cont: true }, 9: { sig: { rushApproved: true } } }, 12)
  check('rush: continue refused while the 急單 card waits', r.at(5).refused && r.at(5).cue === 'rush' && !r.at(5).released, JSON.stringify(r.at(5)))
  check('rush: sim keeps running at the approval wait', r.at(6).paused === false && r.at(6).waiting === 'approval')
  check('rush: the real approval releases it', r.at(9).released && r.at(9.25).cue === 'console', JSON.stringify(r.at(9.25)))
  const n = drive({ 1: { cont: true }, 30: { cont: true } }, 31)
  check('rush: no card at all after 25 s (model failed) → continue may pass', n.at(30).released, JSON.stringify(n.at(30)))
}

// the whole story with the real signals, continue pressed at each checkpoint 4 s after it is ready
function story(times) {
  let tl = D.startTimeline(), sig = {}, lastReady = null
  const out = { fired: [], refused: 0, pausedWhileApproval: false }
  for (let t = 0; t <= 400; t = Math.round((t + 0.25) * 100) / 100) {
    for (const [k, at] of Object.entries(times)) if (t >= at) sig = D.latch(sig, { [k]: true })
    const w = D.waiting(tl, t, sig)
    if (w === 'continue' && lastReady == null) lastReady = t
    if (w !== 'continue') lastReady = null
    const cont = (w === 'continue' && t - lastReady >= 4) || (['h1', 'h2'].includes(D.CUES[tl.index].id) && t % 5 === 0)
    const r = D.advance(tl, t, sig, cont)
    if (r.refused) out.refused++
    if (['h1', 'h2'].includes(D.CUES[r.tl.index].id) && !r.tl.released && D.simPaused(r.tl, t, sig)) out.pausedWhileApproval = true
    if (r.tl.index !== tl.index) { out.fired.push([D.CUES[r.tl.index].id, t, r.tl.fired.at(-1).reason]); lastReady = null }
    tl = r.tl
  }
  out.tl = tl
  return out
}
{
  // morning_rush on main at 40× (morning-scenario's live run): the presenter's checkpoints pause the plant, so these
  // are plant-clock times; story() shifts nothing, it only presses continue 4 s after each checkpoint is ready
  const s = story({ orders: 1, rushCard: 28, rushApproved: 32, preplan: 45, event: 50, approval: 69, approved: 73, worker: 74, rejected: 93, approval2: 104, approved2: 108, workerDone: 120, closed: 109 })
  const at = (id) => s.fired.find((f) => f[0] === id)
  check('story: every cue in order', s.fired.map((f) => f[0]).join(',') === 'rush,console,event,h1,verify,h2,worker,robot,agent,pitch,warn,end', s.fired.map((f) => f[0]).join(','))
  check('h1 / h2: button presses never skip the approvals', s.refused > 0)
  check('h1 / h2: the sim is never paused at an approval', !s.pausedWhileApproval)
  check('verify enters on the real rejection of H1', at('verify')[2] === 'event' && at('verify')[1] === 93, JSON.stringify(at('verify')))
  check('h2 enters on the second card', at('h2')[2] === 'event' && at('h2')[1] === 104, JSON.stringify(at('h2')))
  check('⑤ worker beat on the real 停用檢修 completion (repaired), not on H1\'s 檢修', at('worker')[2] === 'event' && Math.abs(at('worker')[1] - 120) <= 0.5, JSON.stringify(at('worker')))
  check('robot beat right after the worker (at 7 號機), then the Agent page at the desk, then the pitch (CJ\'s first-person order)', s.fired.findIndex((f) => f[0] === 'robot') === s.fired.findIndex((f) => f[0] === 'worker') + 1 && s.fired.findIndex((f) => f[0] === 'pitch') === s.fired.findIndex((f) => f[0] === 'agent') + 1)
  check('the pitch starts on continue at the Agent page', at('pitch')[2] === 'continue', JSON.stringify(at('pitch')))
  const p = s.tl.pitchAt
  check('pitch: 剩 1 分鐘 2:00 after the deck starts, 結束 at 3:00', Math.abs(at('warn')[1] - (p + 120)) < 0.3 && Math.abs(at('end')[1] - (p + 180)) < 0.3, `deck ${p}, warn ${at('warn')?.[1]}, end ${at('end')?.[1]}`)
}

// worker beat: H1's 檢修 being done (crew on the way, even its task 完成) is not the end; only workerDone is
{
  const s = story({ rushCard: 5, rushApproved: 6, preplan: 8, event: 20, approval: 25, approved: 30, worker: 31 })
  const v = s.fired.find((f) => f[0] === 'verify')
  check('no rejection: verify falls back 60 s after the H1 approval', v && v[2] === 'time', JSON.stringify(v))
}

// soft target: 3:00 nudges, 3:30 pitches by itself even mid-story
{
  const r = drive({ 1: { cont: true }, 2: { sig: { rushCard: true } } }, 215)
  let tl = D.startTimeline()
  check('before 3:00: no nudge', !D.overTarget(tl, 179))
  check('at 3:00 in the story: 「該換 Pitch 了」', D.overTarget(tl, 180))
  check('3:30: a story stuck at an approval does not jump by itself', r.at(215).cue === 'rush', JSON.stringify(r.at(215)))
  const c = drive({ 1: { cont: true }, 2: { sig: { rushCard: true } }, 212: { cont: true } }, 213)
  check('after 3:30 continue still cannot pass the approval (no cutting the story)', c.at(212).cue === 'rush' && c.at(212).refused)
  const rb = drive({ 1: { cont: true }, 2: { sig: { rushApproved: true, preplan: true, event: true, approval: true, approved: true, rejected: true, approval2: true, approved2: true, workerDone: true } }, 20: { cont: true }, 30: { cont: true }, 40: { cont: true }, 50: { cont: true }, 60: { cont: true } }, 211)
  check('at the last story beat (Agent page), 3:30 starts the pitch by itself', rb.at(209.75).cue === 'agent' && rb.at(210).cue === 'pitch', JSON.stringify([rb.at(209.75), rb.at(210)]))
  tl = { ...tl, index: D.PITCH_INDEX }
  check('no nudge once pitching', !D.overTarget(tl, 200))
}

// robot show order
{
  const steps = []
  for (let s = 0; s < 7; s += 0.1) { const st = P.showStep(s); if (st && steps.at(-1) !== st) steps.push(st) }
  check('robot show: turn → wave → scan → bow, then done', steps.join(',') === 'turn,wave,scan,bow' && P.showStep(7) === null, steps.join(','))
  check('robot checkpoint lasts at least the show', D.CUES.find((c) => c.id === 'robot').hold.min >= P.ROBOT_SHOW.at(-1)[1])
}

// live signals for H1 → H2 (demo/signals.ts) on fake war-room states
{
  const ev = (state, hyp = []) => ({ id: 'EV-7', machine_id: 'CURE-07', state, opened_at: '2026-10-23T07:12:44+08:00', hypotheses: hyp })
  const ok = (id) => ({ id, event_id: 'EV-7', status: 'approved' })
  const st = (o) => ({ snapshot: { events: [o.ev], machines: [{ machine_id: 'CURE-07', status: 'stopped', repaired: !!o.repaired }] }, pending: o.pending ?? [], resolved: o.resolved ?? [], world: { tasks: { items: o.tasks ?? [] }, people: { workers: [] }, orders: { items: [] } } })
  const seen = SG.freshSeen()
  SG.signals(st({ ev: ev('plan'), resolved: [] }), seen)   // arms: nothing approved before the demo
  const a = SG.signals(st({ ev: ev('await_approval'), pending: [{ id: 'AP-EV-7-1', event_id: 'EV-7' }] }), seen)
  check('H1 card: approval, not approval2', a.approval && !a.approval2 && !a.rejected)
  const b = SG.signals(st({ ev: ev('verify'), resolved: [ok('AP-EV-7-1')], tasks: [{ event_id: 'EV-7', status: '完成' }] }), seen)
  check('after H1: approved, H1 檢修 done is NOT workerDone', b.approved && !b.workerDone && !b.approved2)
  const c = SG.signals(st({ ev: ev('await_approval', [{ status: 'rejected' }, { status: 'active' }]), pending: [{ id: 'AP-EV-7-2', event_id: 'EV-7' }], resolved: [ok('AP-EV-7-1')], tasks: [{ event_id: 'EV-7', status: '完成' }] }), seen)
  check('H1 rejected → rejected + approval2', c.rejected && c.approval2 && !c.approved2)
  const d = SG.signals(st({ ev: ev('closed', [{ status: 'rejected' }]), resolved: [ok('AP-EV-7-1'), ok('AP-EV-7-2')], tasks: [{ event_id: 'EV-7', status: '完成' }] }), seen)
  check('H2 approved, closed, but 停用檢修 not in yet: not workerDone', d.approved2 && !d.workerDone)
  const e = SG.signals(st({ ev: ev('closed', [{ status: 'rejected' }]), resolved: [ok('AP-EV-7-1'), ok('AP-EV-7-2')], tasks: [{ event_id: 'EV-7', status: '完成' }, { event_id: 'EV-7', status: '前往中' }] }), seen)
  check('停用檢修 on the way: not workerDone', !e.workerDone)
  const f = SG.signals(st({ ev: ev('closed', [{ status: 'rejected' }]), repaired: true, resolved: [ok('AP-EV-7-1'), ok('AP-EV-7-2')], tasks: [{ event_id: 'EV-7', status: '完成' }, { event_id: 'EV-7', status: '完成' }] }), seen)
  check('tile repaired (「檢修完成・待恢復運轉」): workerDone, though 7 號機 is still 停用', f.workerDone)
}

// the driver against a fake /api/demo/state (warroom/api/demo.py's rules): continue pressed on any screen moves the
// story one step; a burst of continues (or a pointer run past the end) never leaves the driver stuck
{
  const srv = { running: false, cue_index: 0, cues: [], rev: 0, paused: false, waiting_for: null }
  const io = { mode: 'live', control: async () => {}, demo: async (c) => {
    if (c.action === 'start') Object.assign(srv, { running: true, cue_index: 0, cues: c.cues })
    else if (c.action === 'continue') { if (srv.cue_index + 1 >= srv.cues.length) throw new Error('409 already at the last cue'); srv.cue_index++ }
    else if (c.action === 'cue') Object.assign(srv, { cue_index: c.cue_index, waiting_for: c.waiting_for })
    srv.rev++
    return { ...srv }
  } }
  globalThis.performance ??= { now: () => Date.now() }
  globalThis.fetch = async () => ({ json: async () => [{ name: 'morning_rush' }] })
  const run = new DR.DemoRun(io, '3d')
  let now = 0
  await run.start(now)
  let resolved = []
  const state = () => ({ demo: { ...srv }, snapshot: { events: [], machines: [] }, pending: [], resolved, world: null })
  const tickN = async (n, dt = 250) => { for (let i = 0; i < n; i++) { now += dt; run.tick(now, state()); await new Promise((r) => setTimeout(r, 0)) } }
  await tickN(60)   // ① ready after 12 s
  check('driver: ① waits for continue', D.CUES[run.tl.index].id === 'start' && D.waiting(run.tl, run.t, run.latched) === 'continue')
  await run.continue(); await tickN(4)
  check('driver: one continue (server) → 先核准急單', D.CUES[run.tl.index].id === 'rush', D.CUES[run.tl.index].id)
  check('driver: the server pointer follows the driver', srv.cue_index === run.tl.index)
  for (let i = 0; i < 6; i++) await io.demo({ action: 'continue' }).catch(() => {})   // a burst from another screen
  await tickN(8)
  check('driver: a burst of continues does not skip the rush approval', D.CUES[run.tl.index].id === 'rush' && srv.cue_index === run.tl.index, `${D.CUES[run.tl.index].id} / server ${srv.cue_index}`)
  resolved = [{ id: 'AP-EV-X-RUSH-1', event_id: 'EV-X-RUSH', status: 'approved' }]
  await tickN(4)
  check('driver: the real approval → ②', D.CUES[run.tl.index].id === 'console')
  await tickN(110)   // ② ready after its 25 s fallback
  for (let i = 0; i < 5; i++) await io.demo({ action: 'continue' }).catch(() => {})
  await tickN(8)
  const after = run.tl.index
  await run.continue(); await tickN(8)
  check('driver: after a burst, the next continue still works (no stuck pointer)', run.tl.index !== after || run.tl.released, `${D.CUES[after].id} → ${D.CUES[run.tl.index].id}`)
}

// the 3D follows a demo started on the war room (DemoFollow): the server's cue moves the 3D's beat
{
  const io = { mode: 'live', control: async () => {}, demo: async () => ({}) }
  const f = new DR.DemoFollow(io)
  const st = (cue, waiting = null, running = true) => ({ demo: { running, cue_id: cue, cue_index: D.CUES.findIndex((c) => c.id === cue), elapsed_s: 10, paused: false, waiting_for: waiting, rev: 1 }, snapshot: { events: [], machines: [] }, pending: [], resolved: [], world: null })
  check('follow: same cue, nothing new', f.tick(1000, st('start')) === null && D.CUES[f.tl.index].id === 'start')
  check('follow: the war room moves on → the 3D enters that beat', f.tick(1250, st('rush')) === 'cue' && D.CUES[f.tl.index].id === 'rush')
  check('follow: jumps straight to a later beat too', f.tick(1500, st('worker')) === 'cue' && D.CUES[f.tl.index].id === 'worker')
  f.tick(1600, st('robot'))
  check('follow: a brief step back (the driver re-posting) is ignored', f.tick(1700, st('worker')) === null && D.CUES[f.tl.index].id === 'robot' && f.tick(1800, st('robot')) === null)
  f.tick(1900, st('worker')); f.tick(3500, st('worker'))
  check('follow: a step back that lasts is followed', D.CUES[f.tl.index].id === 'worker')
  f.tick(3750, st('pitch')); f.tick(4000, st('pitch', 'event'))
  check('follow: the pitch released (deck started) when the driver waits for the next event', f.tl.released && f.tl.pitchAt != null)
  check('follow: demo ended elsewhere → stopped', f.tick(4250, st('pitch', null, false)) === 'stopped')
}

// deck manifest
{
  const d = S.parseDeck({ slides: [{ file: '01.png', title: '封面', seconds: 15 }, { file: '02.png', seconds: 25 }, '03.png'] })
  check('deck: objects and plain names', d.slides.join(',') === '/pitch/01.png,/pitch/02.png,/pitch/03.png' && d.seconds.join(',') === '15,25,' && d.titles[0] === '封面')
  check('deck: bare array works', S.parseDeck(['a.png']).slides[0] === '/pitch/a.png')
  check('deck: empty → null (placeholders)', S.parseDeck({ slides: [] }) === null && S.parseDeck(null) === null)
}

// warning-light severity
{
  const ev = (id, machine, state, extra = {}) => ({ id, machine_id: machine, state, opened_at: `2026-10-23T07:${id.slice(-2)}:00+08:00`, anomaly: { kind: 'temp_drop' }, safety_actions: [], ...extra })
  const tiles = [{ machine_id: 'CURE-07', status: 'alarm', temp_c: 122.4 }, { machine_id: 'B-OPEN', status: 'running', temp_c: 96 }, { machine_id: 'A-MIX', status: 'stopped', temp_c: null }]
  const a = A.alarms(tiles, [ev('E-01', 'CURE-07', 'plan'), ev('E-02', 'B-OPEN', 'safe_stop'), ev('E-03', 'PLANT', 'plan')], [])
  check('anomaly: amber with kind and temperature', a['CURE-07']?.level === 'anomaly' && a['CURE-07'].text.includes('溫降') && a['CURE-07'].text.includes('122°C'), a['CURE-07']?.text)
  check('SAFE_STOP: red 安全核心停機', a['B-OPEN']?.level === 'stop' && a['B-OPEN'].text.includes('安全核心停機'))
  check('stopped with no event: red', a['A-MIX']?.level === 'stop')
  check('plant events have no light', !a.PLANT)
  const b = A.alarms(tiles, [ev('E-01', 'CURE-07', 'plan')], [{ event_id: 'E-01' }])
  check('waiting for approval: approval level', b['CURE-07'].level === 'approval' && b['CURE-07'].text.includes('核准'))
  check('resolved: flash for the machine that went quiet', A.resolvedNow(a, {}).includes('CURE-07'))
  check('reduced motion slows the pulse', A.pulseRate(true) < A.pulseRate(false))
}

console.log(failed ? `\n${failed} FAILED` : '\nall passed')
process.exit(failed ? 1 : 0)
