// The demo driver: runs the cue timeline (cues.ts advance()) for whichever screen pressed start — the 3D factory
// (factory3d/useGuidedDemo.ts) or the war room (demo/useDemo.ts) — and keeps everyone else in step through
// /api/demo/state. Each tick it reads the live signals, follows the server's pointer (a continue pressed on another
// screen moves cue_index ahead), pauses the sim while a checkpoint waits for CJ (and plays it again on continue), and
// posts the cue and what it waits for whenever either changes. No React here: the hooks own one DemoRun each.
import type { ScenarioName, SimControl } from '../contracts'
import type { WarRoomState } from '../store'
import {
  BOOST_SPEED, CUES, DEMO_SPEED, FALLBACK_LOADS, MAIN_LOADS, PITCH_AT, PITCH_INDEX, advance, plantWait, clockPause, clockRead, clockResume, clockStart, latch, simPaused,
  startTimeline, waiting, type DemoClock, type DemoLoad, type Signals, type Timeline,
} from './cues'
import { freshSeen, signals, type Seen } from './signals'
import type { DemoCommand, DemoState } from './state'

export interface DemoIO {
  mode: string
  control(c: SimControl): Promise<void>
  demo?(c: DemoCommand): Promise<DemoState>
}

export interface LogRow { id: string; title: string; planned: number; t: number; reason: string; out?: number; outBy?: string; wall: string }

export class DemoRun {
  tl: Timeline = startTimeline()
  clock: DemoClock = { base: 0, since: null }
  t = 0
  seen: Seen = freshSeen()
  latched: Signals = {}
  /** demo second each signal was first seen (the planned-vs-actual table) */
  firstSeen: Record<string, number> = {}
  log: LogRow[] = []
  loads: DemoLoad[] = MAIN_LOADS
  fallback = false
  /** the sim is paused by a checkpoint (we play it again on continue / exit) */
  simHeld = false
  /** the 3D's walk / camera has arrived (cues.ts `arrived`); undefined = no such gate (the war room drives) */
  arrived: boolean | undefined = undefined
  /** the last continue this checkpoint refused (an approval wait) */
  refusedAt = 0
  private loaded = new Set<number>()
  private armedAt = Infinity
  private contReq = false
  private ack = 0
  private posted = ''
  /** the sim speed this driver last set (BOOST_SPEED while the plant is what the demo waits for) */
  speed = 0
  /** the server rev of this driver's own last cue post, and posts still on the way (their echo is not a continue) */
  private myRev = -1
  private inflight = 0
  private serverRunning = false

  private io: DemoIO
  private driver: string
  constructor(io: DemoIO, driver: string) { this.io = io; this.driver = driver }

  private send(c: DemoCommand) { return this.io.demo?.(c).catch((e) => { console.warn('[demo]', e); return null }) ?? Promise.resolve(null) }

  private async load(l: DemoLoad) {
    this.armedAt = Infinity
    this.seen = freshSeen()
    this.latched = {}
    await this.io.control({ action: 'scenario', scenario: l.scenario as ScenarioName, speed: l.speed, preroll: true })
    await this.io.control({ action: 'play' })
    this.armedAt = performance.now() + 1500   // the new plant's snapshot replaces the old one's events
  }

  /** Load the scenario (morning_rush, else the fallback) and tell every screen the demo started. */
  async start(now: number): Promise<string | null> {
    this.tl = startTimeline(); this.clock = clockStart(now); this.t = 0
    this.firstSeen = {}; this.log = [{ id: CUES[0].id, title: CUES[0].title, planned: 0, t: 0, reason: 'time', wall: new Date().toISOString() }]
    this.loaded = new Set([0]); this.ack = 0; this.posted = ''; this.simHeld = false; this.serverRunning = false; this.myRev = -1; this.inflight = 0
    let known = this.io.mode !== 'live'
    if (this.io.mode === 'live') {
      try { known = (await fetch('/api/sim/scenarios').then((r) => r.json()) as { name: string }[]).some((s) => s.name === MAIN_LOADS[0].scenario) } catch { known = true }
    } else known = false
    this.loads = known ? MAIN_LOADS : FALLBACK_LOADS
    this.fallback = !known
    const st = await this.send({ action: 'start', cues: CUES.map((c) => c.id), cue_id: CUES[0].id, cue_index: 0, driver: this.driver, story_target_s: PITCH_AT })
    if (st) this.myRev = st.rev
    try { await this.load(this.loads[0]) } catch (e) { console.warn('[demo] load', e); return '情境載入沒有成功' }
    return known ? null : '「一早訂單湧入」情境還沒上線，改用 7 號機溫降'
  }

  /** Continue (A / Space / N): through the server when there is one, so every screen agrees; else here. */
  async continue(): Promise<'ok' | 'approval'> {
    if (this.io.demo) {
      try { await this.io.demo({ action: 'continue' }); return 'ok' } catch (e) {
        if (/approval/.test(String((e as Error).message ?? e))) { this.refusedAt = performance.now(); return 'approval' }
        // any other refusal (the server's pointer ran past the end, …): this driver's timeline decides; post it again
        this.contReq = true; this.posted = ''; return 'ok'
      }
    }
    this.contReq = true
    return 'ok'
  }

  pause(now: number, paused: boolean) {
    this.clock = paused ? clockPause(this.clock, now) : clockResume(this.clock, now)
    if (this.io.demo) void this.send({ action: paused ? 'pause' : 'resume' })
    else void this.io.control({ action: paused ? 'pause' : 'play' }).catch(() => {})
    if (!paused) this.simHeld = false   // the server played the sim; the next tick re-holds it if a checkpoint still waits
  }

  async exit() {
    if (this.simHeld) { this.simHeld = false; await this.io.control({ action: 'play' }).catch(() => {}) }
    if (this.speed === BOOST_SPEED) await this.io.control({ action: 'speed', speed: this.loads[0]?.speed ?? DEMO_SPEED }).catch(() => {})
    await this.send({ action: 'exit' })
  }

  /** One tick. Returns 'stopped' when another screen ended the demo, 'cue' when the cue changed. */
  tick(now: number, state: WarRoomState): 'stopped' | 'cue' | null {
    const d = state.demo
    if (d?.running) this.serverRunning = true
    if (d && this.serverRunning && !d.running) return 'stopped'
    if (d?.running) {
      // the shared pause (any screen) freezes the demo clock too
      if (d.paused && this.clock.since != null) this.clock = clockPause(this.clock, now)
      if (!d.paused && this.clock.since == null) this.clock = clockResume(this.clock, now)
      // continue pressed anywhere: the server moved the pointer past what this driver has handled
      // (several continues in one burst still move one step: each tick re-posts this driver's cue, which pulls the
      // server's pointer back to it, and `ack` follows what was posted, so the next real continue is seen again)
      if (this.inflight === 0 && d.rev > this.myRev && d.cue_index > this.ack) { this.ack = d.cue_index; this.myRev = d.rev; this.contReq = true; this.posted = '' }
    }
    const t = this.t = clockRead(this.clock, now)
    this.loads.forEach((l, i) => {
      if (i > 0 && t >= l.at && !this.loaded.has(i)) { this.loaded.add(i); void this.load(l).catch((e) => console.warn(e)) }
    })
    const live = now >= this.armedAt ? signals(state, this.seen) : {}
    for (const [k, v] of Object.entries(live)) if (v && this.firstSeen[k] == null) this.firstSeen[k] = Math.round(t * 10) / 10
    this.latched = latch(this.latched, live)
    const sig = this.arrived === undefined ? this.latched : { ...this.latched, arrived: this.arrived }
    const before = this.tl
    let r = advance(before, t, sig, this.contReq)
    this.contReq = false
    // a continue that ends a hold whose next beat starts right away: take that step in the same tick, so the server
    // never sees the old beat again in between (every screen would flash back to it)
    if (r.tl.released && !before.released && r.tl.index === before.index) {
      const r2 = advance(r.tl, t, sig, false)
      if (r2.tl.index !== r.tl.index) r = r2
    }
    // refused (an approval, or a live event still to come): post the cue again, so a server that moved ahead comes back
    if (r.refused) { this.refusedAt = now; this.posted = '' }
    this.tl = r.tl
    const changed = r.tl.index !== before.index
    if (changed) {
      const f = r.tl.fired[r.tl.fired.length - 1], c = CUES[r.tl.index]
      const prev = r.tl.fired[r.tl.fired.length - 2]
      const row = this.log.find((x) => x.id === prev?.id && x.out == null)
      if (row && prev?.out != null) { row.out = Math.round(prev.out * 10) / 10; row.outBy = prev.outBy }
      this.log.push({ id: c.id, title: c.title, planned: c.at, t: Math.round(f.t * 10) / 10, reason: f.reason, wall: new Date().toISOString() })
    } else if (r.tl.released && !before.released) {
      const f = r.tl.fired[r.tl.fired.length - 1], row = this.log[this.log.length - 1]
      if (row && f.out != null) { row.out = Math.round(f.out * 10) / 10; row.outBy = f.outBy }
    }
    // a checkpoint waiting for CJ holds the plant still (never at the approval: it needs the plant running)
    const hold = simPaused(r.tl, t, sig) && this.clock.since != null
    if (hold !== this.simHeld) {
      this.simHeld = hold
      void this.io.control({ action: hold ? 'pause' : 'play' }).catch(() => {})
    }
    // waiting on the plant (not on CJ): run the sim fast; back to the demo speed once the beat is there
    const speed = hold || !this.io.demo ? null : plantWait(r.tl, sig) ? BOOST_SPEED : this.loads[0]?.speed ?? DEMO_SPEED
    if (speed != null && speed !== this.speed && this.clock.since != null && now >= this.armedAt) {
      this.speed = speed
      void this.io.control({ action: 'speed', speed }).catch(() => {})
    }
    const w = waiting(r.tl, t, sig)
    // done with a beat and waiting for the next one's live event: the shared pointer already shows that next beat
    // (waiting for its event), so after a continue no screen flashes back to the beat that just ended
    const postIndex = r.tl.released && CUES[r.tl.index + 1]?.enter?.on ? r.tl.index + 1 : r.tl.index
    const key = `${postIndex}|${w}`
    if (key !== this.posted) {
      this.posted = key
      this.ack = postIndex
      this.inflight++
      void this.send({ action: 'cue', cue_id: CUES[postIndex].id, cue_index: postIndex, waiting_for: w })
        .then((out) => { if (out) this.myRev = Math.max(this.myRev, out.rev) })
        .finally(() => { this.inflight-- })
    }
    return changed ? 'cue' : null
  }
}

/** The 3D when another screen drives (the war room's 「▶ Demo 模式」): no timeline of its own, it follows the server's
 *  cue (GET/POST /api/demo/state, WS "demo") and sends continue / pause / exit like any other screen. Same surface as
 *  DemoRun, so the 3D's camera, walk and monitor code runs the same for both. */
export class DemoFollow {
  tl: Timeline = startTimeline()
  t = 0
  seen: Seen = freshSeen()
  latched: Signals = {}
  firstSeen: Record<string, number> = {}
  log: LogRow[] = []
  fallback = false
  simHeld = false
  refusedAt = 0
  arrived: boolean | undefined = undefined
  private io: DemoIO
  private base = { t: 0, at: 0 }
  private backSince: number | null = null
  constructor(io: DemoIO) { this.io = io }

  async continue(): Promise<'ok' | 'approval'> {
    try { await this.io.demo?.({ action: 'continue' }); return 'ok' } catch (e) {
      this.refusedAt = performance.now()
      return /approval/.test(String((e as Error).message ?? e)) ? 'approval' : 'ok'
    }
  }
  pause(_now: number, paused: boolean) { void this.io.demo?.({ action: paused ? 'pause' : 'resume' }).catch(() => {}) }
  async exit() { await this.io.demo?.({ action: 'exit' }).catch(() => null) }

  tick(now: number, state: WarRoomState): 'stopped' | 'cue' | null {
    const d = state.demo
    if (!d?.running) return 'stopped'
    // the server's elapsed time, carried forward between pushes
    if (d.elapsed_s !== this.base.t) this.base = { t: d.elapsed_s, at: now }
    const t = this.t = d.paused ? d.elapsed_s : d.elapsed_s + (now - this.base.at) / 1000
    const live = signals(state, this.seen)
    for (const [k, v] of Object.entries(live)) if (v && this.firstSeen[k] == null) this.firstSeen[k] = Math.round(t * 10) / 10
    this.latched = latch(this.latched, live)
    let i = Math.max(0, CUES.findIndex((c) => c.id === d.cue_id))
    const released = d.waiting_for === 'event'
    // the driver may pull the pointer back one beat for a moment after a continue (it still waits for that beat's
    // next event): a step back only counts once it has lasted 1.5 s, so the 3D does not walk back and forth
    if (i < this.tl.index) {
      this.backSince ??= now
      if (now - this.backSince < 1500) i = this.tl.index
    } else this.backSince = null
    if (i !== this.tl.index) {
      const c = CUES[i]
      this.tl = {
        index: i, enteredAt: t, released, releasedAt: t, pitchAt: i > PITCH_INDEX ? this.tl.pitchAt ?? t : null,
        fired: [...this.tl.fired, { id: c.id, t, reason: 'follow' }],
      }
      this.log.push({ id: c.id, title: c.title, planned: c.at, t: Math.round(t * 10) / 10, reason: 'follow', wall: new Date().toISOString() })
      return 'cue'
    }
    if (released && !this.tl.released) this.tl = { ...this.tl, released: true, releasedAt: t, pitchAt: this.tl.index === PITCH_INDEX ? t : this.tl.pitchAt }
    return null
  }
}
