// The demo driver: runs the cue timeline (cues.ts advance()) for whichever screen pressed start — the 3D factory
// (factory3d/useGuidedDemo.ts) or the war room (demo/useDemo.ts) — and keeps everyone else in step through
// /api/demo/state. Each tick it reads the live signals, follows the server's pointer (a continue pressed on another
// screen moves cue_index ahead), pauses the sim while a checkpoint waits for CJ (and plays it again on continue), and
// posts the cue and what it waits for whenever either changes. No React here: the hooks own one DemoRun each.
import type { ScenarioName, SimControl } from '../contracts'
import type { WarRoomState } from '../store'
import {
  CUES, FALLBACK_LOADS, MAIN_LOADS, PITCH_AT, advance, clockPause, clockRead, clockResume, clockStart, latch, simPaused,
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
  /** the last continue this checkpoint refused (an approval wait) */
  refusedAt = 0
  private loaded = new Set<number>()
  private armedAt = Infinity
  private contReq = false
  private ack = 0
  private posted = ''
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
    this.loaded = new Set([0]); this.ack = 0; this.posted = ''; this.simHeld = false; this.serverRunning = false
    let known = this.io.mode !== 'live'
    if (this.io.mode === 'live') {
      try { known = (await fetch('/api/sim/scenarios').then((r) => r.json()) as { name: string }[]).some((s) => s.name === MAIN_LOADS[0].scenario) } catch { known = true }
    } else known = false
    this.loads = known ? MAIN_LOADS : FALLBACK_LOADS
    this.fallback = !known
    await this.send({ action: 'start', cues: CUES.map((c) => c.id), cue_id: CUES[0].id, cue_index: 0, driver: this.driver, story_target_s: PITCH_AT })
    try { await this.load(this.loads[0]) } catch (e) { console.warn('[demo] load', e); return '情境載入沒有成功' }
    return known ? null : '「一早訂單湧入」情境還沒上線，改用 7 號機溫降'
  }

  /** Continue (A / Space / N): through the server when there is one, so every screen agrees; else here. */
  async continue(): Promise<'ok' | 'approval'> {
    if (this.io.demo) {
      try { await this.io.demo({ action: 'continue' }); return 'ok' } catch (e) {
        if (/approval|409/.test(String((e as Error).message ?? e))) { this.refusedAt = performance.now(); return 'approval' }
        this.contReq = true; return 'ok'
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
      if (d.cue_index > this.ack) { this.ack = d.cue_index; this.contReq = true }
    }
    const t = this.t = clockRead(this.clock, now)
    this.loads.forEach((l, i) => {
      if (i > 0 && t >= l.at && !this.loaded.has(i)) { this.loaded.add(i); void this.load(l).catch((e) => console.warn(e)) }
    })
    const live = now >= this.armedAt ? signals(state, this.seen) : {}
    for (const [k, v] of Object.entries(live)) if (v && this.firstSeen[k] == null) this.firstSeen[k] = Math.round(t * 10) / 10
    this.latched = latch(this.latched, live)
    const sig = this.latched
    const before = this.tl
    const r = advance(before, t, sig, this.contReq)
    this.contReq = false
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
    const w = waiting(r.tl, t, sig)
    const key = `${r.tl.index}|${w}`
    if (key !== this.posted) {
      this.posted = key
      this.ack = Math.max(this.ack, r.tl.index)
      void this.send({ action: 'cue', cue_id: CUES[r.tl.index].id, cue_index: r.tl.index, waiting_for: w })
    }
    return changed ? 'cue' : null
  }
}
