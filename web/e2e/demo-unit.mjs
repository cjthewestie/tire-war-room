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
await vite.close()

const ids = D.CUES.map((c) => c.id)
check('cue order', ids.join(',') === 'start,rush,console,event,phone,worker,agent,robot,pitch,warn,end', ids.join(','))
check('eight presenter checkpoints ①–⑧ (the 急單 approval is extra)', D.CHECKPOINTS.join(',') === 'start,console,event,phone,worker,agent,robot,pitch', D.CHECKPOINTS.join(','))
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
    const cont = (w === 'continue' && t - lastReady >= 4) || (D.CUES[tl.index].id === 'phone' && t % 5 === 0)
    const r = D.advance(tl, t, sig, cont)
    if (r.refused) out.refused++
    if (D.CUES[r.tl.index].id === 'phone' && !r.tl.released && D.simPaused(r.tl, t, sig)) out.pausedWhileApproval = true
    if (r.tl.index !== tl.index) { out.fired.push([D.CUES[r.tl.index].id, t, r.tl.fired.at(-1).reason]); lastReady = null }
    tl = r.tl
  }
  out.tl = tl
  return out
}
{
  const s = story({ orders: 1, rushCard: 20, rushApproved: 27, preplan: 45, event: 62, approval: 70, approved: 90, worker: 92, workerDone: 96, closed: 97 })
  const at = (id) => s.fired.find((f) => f[0] === id)
  check('story: every cue in order', s.fired.map((f) => f[0]).join(',') === 'rush,console,event,phone,worker,agent,robot,pitch,warn,end', s.fired.map((f) => f[0]).join(','))
  check('④ phone: button presses never skip it', s.refused > 0 && at('worker')[1] >= 96, `refused ${s.refused}, worker at ${at('worker')?.[1]}`)
  check('④ the sim is never paused at the approval', !s.pausedWhileApproval)
  check('⑤ worker beat fires on the real task completion (workerDone), not before', at('worker')[2] === 'event' && at('worker')[1] === 96, JSON.stringify(at('worker')))
  check('⑦ robot beat comes before the pitch, right after the Agent page', s.fired.findIndex((f) => f[0] === 'robot') === s.fired.findIndex((f) => f[0] === 'agent') + 1 && at('pitch')[1] > at('robot')[1])
  check('⑧ the pitch starts on continue at the robot (before 3:30)', at('pitch')[2] === 'continue' && at('pitch')[1] < 210, JSON.stringify(at('pitch')))
  const p = s.tl.pitchAt
  check('pitch: 剩 1 分鐘 2:00 after the deck starts, 結束 at 3:00', Math.abs(at('warn')[1] - (p + 120)) < 0.3 && Math.abs(at('end')[1] - (p + 180)) < 0.3, `deck ${p}, warn ${at('warn')[1]}, end ${at('end')[1]}`)
}

// worker beat: crew on the way is not enough; the task has to be done
{
  const s = story({ rushCard: 5, rushApproved: 6, preplan: 8, event: 20, approval: 25, approved: 30, worker: 31 })
  const w = s.fired.find((f) => f[0] === 'worker')
  check('worker beat waits for workerDone (falls back 45 s after the approval, not on 前往中)', w && w[2] === 'time' && w[1] >= 75, JSON.stringify(w))
}

// soft target: 3:00 nudges, 3:30 pitches by itself even mid-story
{
  const r = drive({ 1: { cont: true }, 2: { sig: { rushCard: true } } }, 215)
  let tl = D.startTimeline()
  check('before 3:00: no nudge', !D.overTarget(tl, 179))
  check('at 3:00 in the story: 「該換 Pitch 了」', D.overTarget(tl, 180))
  check('3:30: a story stuck at an approval does not jump by itself', r.at(215).cue === 'rush', JSON.stringify(r.at(215)))
  const c = drive({ 1: { cont: true }, 2: { sig: { rushCard: true } }, 205: { cont: true }, 212: { cont: true } }, 213)
  check('before 3:30 continue cannot pass the approval; after 3:30 it goes straight to the pitch', c.at(205).cue === 'rush' && c.at(212).cue === 'pitch', JSON.stringify([c.at(205), c.at(212)]))
  const rb = drive({ 1: { cont: true }, 2: { sig: { rushApproved: true, preplan: true, event: true, approval: true, approved: true, workerDone: true } }, 20: { cont: true }, 30: { cont: true }, 50: { cont: true }, 60: { cont: true } }, 211)
  check('at the robot beat, 3:30 starts the pitch by itself', rb.at(209.75).cue === 'robot' && rb.at(210).cue === 'pitch', JSON.stringify([rb.at(209.75), rb.at(210)]))
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
