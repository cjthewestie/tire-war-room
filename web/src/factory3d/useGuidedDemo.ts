// The 「▶ Demo（6 分鐘）」 button's guided run (CJ, 2026-10-22): loads the morning_rush scenario at demo speed, then walks
// the presenter through the story with big cue cards (主控台 → 事件 → 手機核准 → 員工到場 → Agent 頁面), flies the camera,
// and at 3:00 switches to the pitch on the whiteboard with a 6:00 countdown. The timeline itself is demoCues.ts; this
// file feeds it the clock and the live signals, runs the scenario loads and draws the cards and the control bar.
// Keys (FactoryApp; no clash with the 3D keys: D strafes and P raises the phone, so the demo uses G / N / B):
// G start / restart, N next cue, B pause / resume, ← / → (or PageUp / PageDown) change slides in the pitch.
// The cards and the bar are GuidedDemo.tsx.
import { useCallback, useEffect, useRef, useState } from 'react'
import type { ScenarioName } from '../contracts'
import { SCENARIOS } from '../mock/engine'
import { primaryEvent, type WarRoomState } from '../store'
import type { Transport } from '../transport'
import { PLACED_BY_ID, WHITEBOARD } from './layout'
import { director } from './pose'
import { robot } from './patrol'
import {
  CUES, DEMO_TOTAL, FALLBACK_LOADS, MAIN_LOADS, clockPause, clockRead, clockResume, clockSet, clockStart,
  current, inPitch, mss, skip, skipTime, startTimeline, step, upcoming, type DemoClock, type DemoLoad, type Shot, type Signals, type Timeline,
} from './demoCues'

const OVERVIEW = { x: 1.6, z: 0.4 }

let ac: AudioContext | null = null
function chime(loud = false) {
  try {
    ac ??= new AudioContext()
    if (ac.state === 'suspended') void ac.resume().catch(() => {})
    const t0 = ac.currentTime
    ;(loud ? [659, 880, 1319] : [880, 1175]).forEach((f, i) => {
      const o = ac!.createOscillator(), g = ac!.createGain()
      o.type = 'sine'; o.frequency.value = f
      o.connect(g); g.connect(ac!.destination)
      const s = t0 + i * 0.16
      g.gain.setValueAtTime(0.0001, s)
      g.gain.exponentialRampToValueAtTime(0.18, s + 0.03)
      g.gain.exponentialRampToValueAtTime(0.0001, s + 0.9)
      o.start(s); o.stop(s + 0.95)
    })
  } catch { /* no audio */ }
}

/** Point the overview camera for a cue. */
function aim(shot: Shot, machine: string | null) {
  director.on = true
  if (shot.kind === 'whiteboard') {
    Object.assign(director, { x: WHITEBOARD.x, y: WHITEBOARD.y, z: WHITEBOARD.z, yaw: WHITEBOARD.yaw, pitch: 0.2, dist: 3.9 })
    return
  }
  const p = shot.kind === 'incident' && machine ? PLACED_BY_ID[machine] : null
  const close = shot.kind === 'incident' && !!shot.close
  if (!p) { Object.assign(director, { x: OVERVIEW.x, y: 0.55, z: OVERVIEW.z, yaw: 0.35, pitch: 0.82, dist: 25 }); return }
  // from the aisle side of the machine (presses: from the north), a little off-axis
  const north = p.line === 'CURE' || p.line === 'D' || p.line === 'BOND'
  Object.assign(director, { x: p.x, y: 0.5, z: p.z + (north ? -0.9 : 0.9), yaw: (north ? Math.PI : 0) - 0.45, pitch: close ? 0.62 : 0.78, dist: close ? 6.5 : 10 })
}

/** The live facts the cues wait for. `seen` remembers the incident the demo is following. */
function signals(state: WarRoomState, seen: { id: string | null; machine: string | null }): Signals {
  const events = state.snapshot?.events ?? []
  const open = events.filter((e) => e.state !== 'closed' && e.state !== 'safe_stop')
  const ev = primaryEvent(open)
  if (ev && !seen.id) { seen.id = ev.id; seen.machine = ev.machine_id }
  const followed = seen.id ? events.find((e) => e.id === seen.id) ?? null : null
  const workers = state.world?.people.workers ?? []
  const onTask = !!seen.machine && workers.some((w) => w.machine_id === seen.machine && (w.state === '前往中' || w.state === '執行中'))
  return {
    orders: (state.world?.orders.new_today ?? 0) > 0,
    event: !!ev,
    approval: state.pending.length > 0,
    worker: onTask || (!!followed && (followed.state === 'execute' || followed.state === 'verify')),
    closed: !!seen.id && (!followed || followed.state === 'closed'),
  }
}

export interface DemoLogRow { id: string; title: string; planned: number; t: number; reason: string; wall: string }

export function useGuidedDemo({ state, transport, onBegin, onEnd }: {
  state: WarRoomState; transport: Transport | null
  /** the demo starts: leave first person, close panels */
  onBegin: () => void
  onEnd: () => void
}) {
  const [running, setRunning] = useState(false)
  const [tl, setTl] = useState<Timeline>(startTimeline)
  const [clock, setClock] = useState<DemoClock>({ base: 0, since: null })
  const [t, setT] = useState(0)
  const [slide, setSlide] = useState(0)
  const [slideView, setSlideView] = useState(false)
  const [plan, setPlan] = useState<{ loads: DemoLoad[]; fallback: boolean } | null>(null)
  const [bigUntil, setBigUntil] = useState(0)
  const [note, setNote] = useState<string | null>(null)
  const [incidentEventId, setIncidentEventId] = useState<string | null>(null)
  const loaded = useRef(new Set<number>())
  const armedAt = useRef(Infinity)
  const seen = useRef<{ id: string | null; machine: string | null }>({ id: null, machine: null })
  const log = useRef<DemoLogRow[]>([])
  const stateRef = useRef(state)
  useEffect(() => { stateRef.current = state }, [state])

  const load = useCallback(async (l: DemoLoad) => {
    if (!transport) return
    armedAt.current = Infinity
    seen.current = { id: null, machine: null }
    await transport.control({ action: 'scenario', scenario: l.scenario as ScenarioName, speed: l.speed, preroll: true })
    await transport.control({ action: 'play' })
    armedAt.current = performance.now() + 1500   // the new plant's snapshot replaces the old one's events
  }, [transport])

  const begin = useCallback(async () => {
    if (!transport) return
    onBegin()
    chime()
    loaded.current = new Set([0])
    log.current = []
    setTl(startTimeline()); setSlide(0); setSlideView(false); setT(0); setBigUntil(0)
    const now = performance.now()
    setClock(clockStart(now))
    setRunning(true)
    aim(CUES[0].shot, null)
    log.current.push({ id: CUES[0].id, title: CUES[0].title, planned: 0, t: 0, reason: 'time', wall: new Date().toISOString() })
    // morning_rush if the server has it, else the 7 號機 fallback (normal → press7_temp_drop at 1:02)
    const known = transport.mode === 'mock' ? SCENARIOS.some((s) => s.name === MAIN_LOADS[0].scenario) : true
    try {
      if (!known) throw new Error('not in this build')
      await load(MAIN_LOADS[0])
      setPlan({ loads: MAIN_LOADS, fallback: false })
    } catch (e) {
      console.warn('[demo] morning_rush unavailable, falling back', e)
      setNote('「一早訂單湧入」情境還沒上線，改用 7 號機溫降')
      setPlan({ loads: FALLBACK_LOADS, fallback: true })
      try { await load(FALLBACK_LOADS[0]) } catch (e2) { console.warn(e2); setNote('情境載入沒有成功') }
    }
  }, [transport, load, onBegin])

  const stop = useCallback(() => {
    setRunning(false); setSlideView(false)
    director.on = false
    onEnd()
  }, [onEnd])

  const paused = running && clock.since == null
  const togglePause = useCallback(() => {
    if (!running) return
    const now = performance.now()
    if (clock.since == null) {
      setClock(clockResume(clock, now))
      if (!inPitch(clockRead(clock, now))) void transport?.control({ action: 'play' }).catch(() => {})
    } else {
      setClock(clockPause(clock, now))
      if (!inPitch(clockRead(clock, now))) void transport?.control({ action: 'pause' }).catch(() => {})
    }
  }, [running, clock, transport])

  const next = useCallback(() => {
    if (!running) return
    const now = performance.now()
    const tt = clockRead(clock, now)
    const n = skip(tl, tt)
    if (n === tl) return
    const t2 = skipTime(n, tt)
    if (t2 !== tt) setClock(clockSet(clock, t2, now))
    setTl(n)
  }, [running, clock, tl])

  // the tick: clock, scenario loads, signals → timeline
  useEffect(() => {
    if (!running) return
    const id = setInterval(() => {
      const now = performance.now()
      const tt = Math.min(clockRead(clock, now), DEMO_TOTAL + 0.5)
      setT(tt)
      if (plan) plan.loads.forEach((l, i) => {
        if (i > 0 && tt >= l.at && !loaded.current.has(i)) { loaded.current.add(i); void load(l).catch((e) => console.warn(e)) }
      })
      const sig = now >= armedAt.current ? signals(stateRef.current, seen.current) : {}
      setTl((cur) => step(cur, tt, sig))
    }, 250)
    return () => clearInterval(id)
  }, [running, clock, plan, load])

  // a new cue: camera, chime, log
  const cue = current(tl)
  const lastCue = useRef<string>('')
  useEffect(() => {
    if (!running || cue.id === lastCue.current) return
    lastCue.current = cue.id
    const f = tl.fired[tl.fired.length - 1]
    if (cue.id !== 'start') log.current.push({ id: cue.id, title: cue.title, planned: cue.at, t: Math.round(f.t * 10) / 10, reason: f.reason, wall: new Date().toISOString() })
    console.info(`[demo] ${mss(f.t)} ${cue.id} (${f.reason}) ${cue.title}`)
    aim(cue.shot, seen.current.machine ?? robot.target)
    setIncidentEventId(seen.current.id)
    if (cue.big) { chime(true); setBigUntil(performance.now() + 4000) } else if (cue.id !== 'start') chime()
  }, [running, cue, tl.fired])
  // follow the incident machine once it is known (the event cue may fire before the camera knew where)
  useEffect(() => {
    if (running && cue.shot.kind === 'incident') aim(cue.shot, seen.current.machine ?? robot.target)
  }, [running, cue, t])
  useEffect(() => () => { director.on = false }, [])
  useEffect(() => { if (!running) lastCue.current = '' }, [running])

  // test hook (headless e2e only)
  useEffect(() => {
    if (!import.meta.env.DEV && !new URLSearchParams(location.search).has('test')) return
    const w = window as unknown as Record<string, unknown>
    w.__demo3d = () => ({ running, t, cue: cue.id, paused, slide, slideView, fallback: plan?.fallback ?? null, log: log.current, seen: seen.current, director: { ...director } })
    return () => { delete w.__demo3d }
  }, [running, t, cue.id, paused, slide, slideView, plan])

  return {
    running, t, cue, next: upcoming(tl), paused, slide, setSlide, slideView, setSlideView, bigUntil, note, setNote,
    begin, stop, togglePause, skip: next, fallback: plan?.fallback ?? false, incidentEventId,
  }
}
export type GuidedDemo = ReturnType<typeof useGuidedDemo>

