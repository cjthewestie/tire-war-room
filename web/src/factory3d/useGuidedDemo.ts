// The 「▶ Demo（6 分鐘）」 run in the 3D factory (CJ, 2026-10-22: 「demo按鍵按下去就只能按照流程跑幫我固定視角」,
// 「固定視角的意思是幫我安排連貫的視角」, 「走動按鍵關鍵處可以讓我按按鍵再繼續」). The timeline and the sim pausing are
// the shared driver (demo/driver.ts, the same one the war room uses); this hook adds the 3D's part: the one-take camera
// (demoCamera.ts) on each beat's shot, the robot's little show at the robot beat, the chime, the pitch slides, the
// exit confirm. While it runs, the camera is locked: only continue / pause / exit / slides (and the phone) work.
// Keys (FactoryApp, and the same actions on the Xbox pad, gamepad.ts): G start · Space / N continue · B pause ·
// Esc exit (confirm) · ← → / PageUp PageDown slides · P phone.
import { useCallback, useEffect, useRef, useState } from 'react'
import { DemoRun, type LogRow } from '../demo/driver'
import { AUTO_PITCH, CHECKPOINTS, CUES, type Shot, STORY_TARGET, current, overTarget, pitchLeft, ready, upcoming, waiting } from '../demo/cues'
import type { WarRoomState } from '../store'
import type { Transport } from '../transport'
import { demoCam } from './demoCamera'
import { ROBOT_MEET, stationFor, walker, type StationId } from './demoWalk'
import { START } from './layout'
import { teleport } from './pose'
import { robot } from './patrol'
import { director } from './pose'

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

/** Which page the war-room computer shows at a beat (the lead has to be at the desk). */
function monitorFor(cueId: string, eventId: string | null): string | null {
  if (cueId === 'rush' || cueId === 'console') return '#agent'
  if (cueId === 'agent') return eventId ? `#console?event=${encodeURIComponent(eventId)}` : '#console'
  return null
}

interface Snap { tl: DemoRun['tl']; t: number; sig: DemoRun['latched']; eventId: string | null; fallback: boolean; log: LogRow[] }
const snapOf = (r: DemoRun): Snap => ({ tl: r.tl, t: r.t, sig: r.arrived === undefined ? r.latched : { ...r.latched, arrived: r.arrived }, eventId: r.seen.id, fallback: r.fallback, log: [...r.log] })

export function useGuidedDemo({ state, transport, onBegin, onEnd, onWalk, nSlides }: {
  state: WarRoomState; transport: Transport | null
  /** the overview hands over to the first-person walk (FactoryApp: first-person view) */
  onWalk: () => void
  /** the demo starts: leave first person, close panels */
  onBegin: () => void
  onEnd: () => void
  nSlides: number
}) {
  const run = useRef<DemoRun | null>(null)
  const [running, setRunning] = useState(false)
  /** what the overlay shows, copied out of the driver every tick */
  const [snap, setSnap] = useState<Snap | null>(null)
  const [slide, setSlide] = useState(0)
  const [slideView, setSlideView] = useState(false)
  const [bigUntil, setBigUntil] = useState(0)
  const [note, setNote] = useState<string | null>(null)
  const [confirmExit, setConfirmExit] = useState(false)
  const [paused, setPaused] = useState(false)
  const lastRefused = useRef(0)
  const walk = useRef<{ station: StationId | null; monitorAt: number; enteredAt: number }>({ station: null, monitorAt: 0, enteredAt: 0 })
  const [monitor, setMonitor] = useState<string | null>(null)
  const monitorRef = useRef<string | null>(null)
  const slideOpened = useRef(false)
  const stateRef = useRef(state)
  useEffect(() => { stateRef.current = state }, [state])

  const stop = useCallback(() => {
    run.current = null
    setSnap(null)
    setMonitor(null); monitorRef.current = null
    walker.reset()
    setRunning(false); setSlideView(false); setConfirmExit(false); setPaused(false)
    director.on = false
    onEnd()
  }, [onEnd])

  const begin = useCallback(async () => {
    if (!transport) return
    if (run.current) await run.current.exit()
    onBegin()
    chime()
    const r = new DemoRun(transport, '3d')
    run.current = r
    demoCam.reset()
    demoCam.reduced = typeof matchMedia !== 'undefined' && matchMedia('(prefers-reduced-motion: reduce)').matches
    walker.reset()
    // the lead starts inside the war-room door, like a fresh start of the 3D (the walk goes from there)
    teleport(START.x, START.z, START.yaw)
    director.on = true
    director.mode = 'orbit'
    walk.current = { station: null, monitorAt: 0, enteredAt: 0 }
    setMonitor(null)
    setSlide(0); setSlideView(false); setBigUntil(0); setConfirmExit(false); setPaused(false); setNote(null)
    setRunning(true)
    setSnap(snapOf(r))
    const n = await r.start(performance.now())
    if (n) setNote(n)
  }, [transport, onBegin])

  /** A / Space / N. At the approval checkpoint it is refused: the approval has to happen on the phone. */
  const cont = useCallback(async () => {
    const r = run.current
    if (!r || paused) return
    if (confirmExit) { setConfirmExit(false); return }
    const res = await r.continue()
    if (res === 'approval') setNote('要先核准（手機或核准卡），才能往下')
  }, [paused, confirmExit])

  const togglePause = useCallback(() => {
    const r = run.current
    if (!r) return
    r.pause(performance.now(), !paused)
    setPaused(!paused)
  }, [paused])

  const exit = useCallback(async () => {
    const r = run.current
    if (r) await r.exit()
    stop()
  }, [stop])

  // the tick: the shared driver, then the 3D's part of a new cue
  useEffect(() => {
    if (!running) return
    const id = setInterval(() => {
      const r = run.current
      if (!r) return
      const out = r.tick(performance.now(), stateRef.current)
      if (out === 'stopped') { stop(); return }
      const c = current(r.tl)
      const now = performance.now()
      const w = walk.current
      if (out === 'cue') {
        console.info(`[demo] ${r.t.toFixed(1)} ${c.id} (${r.tl.fired[r.tl.fired.length - 1].reason})`)
        if (c.big) { chime(true); setBigUntil(now + 4000) } else chime()
        if (c.id === 'pitch') setSlide(0)
        w.enteredAt = now
        w.station = stationFor(c.id, c.shot)
        // the robot comes over to the lead at 7 號機, then plays its show facing the camera
        if (c.id === 'robot') robot.visitReq = ROBOT_MEET
      }
      // camera: the opening overview (orbit), a hand-over onto the lead, then the first-person walk between stations
      if (director.mode === 'orbit') {
        if (c.shot.kind === 'establish') demoCam.setShot(c.shot, null)
        else {
          demoCam.setShot({ kind: 'lead' } as unknown as Shot, null)
          if (!demoCam.flying && now - w.enteredAt > 600) { director.mode = 'walk'; onWalk() }
        }
      }
      if (director.mode === 'walk' && w.station && walker.at !== w.station) walker.go(w.station, { run: c.id === 'worker' })
      // the war-room computer: open at the desk once the lead has arrived (主控台 for the orders, Agent page later)
      const want = monitorFor(c.id, r.seen.id)
      const atDesk = director.mode === 'walk' && walker.at === 'desk' && walker.done
      const show = want && atDesk ? want : null
      if (show !== monitorRef.current) { monitorRef.current = show; setMonitor(show); w.monitorAt = now }
      // the pitch: standing at the board, continue opens the slides full screen
      if (c.id === 'pitch' && r.tl.released && !slideOpened.current) { slideOpened.current = true; setSlideView(true) }
      if (c.id !== 'pitch' && c.id !== 'warn' && c.id !== 'end') slideOpened.current = false
      // a checkpoint is ready only once the walk (and the monitor, the robot's show) has arrived
      const robotDone = c.id !== 'robot' || robot.played.includes('bow') || now - w.enteredAt > 30000
      const monitorDone = !want || (!!show && now - w.monitorAt > 1500)
      r.arrived = director.mode === 'walk' ? walker.done && robotDone && monitorDone : c.shot.kind === 'establish' ? undefined : false
      if (r.refusedAt > lastRefused.current) {
        lastRefused.current = r.refusedAt
        const w = waiting(r.tl, r.t, r.latched)
        setNote(w === 'approval' ? '要先核准（手機或核准卡），才能往下' : r.arrived === false ? '還在走過去，到了再按' : '等工廠下一件事發生，不能跳過')
      }
      const d = stateRef.current.demo
      if (d?.running && d.paused !== paused) setPaused(d.paused)
      setSnap(snapOf(r))
    }, 250)
    return () => clearInterval(id)
  }, [running, stop, paused, onWalk])
  useEffect(() => () => { director.on = false }, [])

  const tl = snap?.tl
  const t = snap?.t ?? 0
  const cue = tl ? current(tl) : CUES[0]
  const sig = snap?.sig ?? {}
  const w = tl ? waiting(tl, t, sig) : null
  const isReady = !!tl && !tl.released && ready(tl, t, sig)
  const changeSlide = useCallback((d: number) => setSlide((i) => Math.max(0, Math.min(nSlides - 1, i + d))), [nSlides])

  // test hook (headless e2e only)
  useEffect(() => {
    if (!import.meta.env.DEV && !new URLSearchParams(location.search).has('test')) return
    const w2 = window as unknown as Record<string, unknown>
    w2.__demo3d = () => {
      const x = run.current
      return x ? {
        running: true, t: x.t, cue: current(x.tl).id, index: x.tl.index, released: x.tl.released,
        waiting: waiting(x.tl, x.t, x.arrived === undefined ? x.latched : { ...x.latched, arrived: x.arrived }),
        simHeld: x.simHeld, paused, slide, slideView, fallback: x.fallback, log: x.log, signals: x.firstSeen, seen: { id: x.seen.id, machine: x.seen.machine },
        walk: { mode: director.mode, at: walker.at, done: walker.done }, monitor: monitorRef.current, arrived: x.arrived,
        camMaxStep: demoCam.maxStep, flying: demoCam.flying, robotPlayed: [...robot.played], camTraceLen: demoCam.trace.length,
      } : { running: false, director: director.on }
    }
    w2.__demoCamTrace = () => demoCam.trace
    return () => { delete w2.__demo3d; delete w2.__demoCamTrace }
  }, [paused, slide, slideView])

  return {
    running, t, cue, monitor, next: tl ? upcoming(tl) : null, waiting: w, ready: isReady, released: !!tl?.released,
    paused, slide, setSlide, changeSlide, slideView, setSlideView, bigUntil, note, setNote, confirmExit, setConfirmExit,
    begin, stop, exit, cont, togglePause, fallback: snap?.fallback ?? false, incidentEventId: snap?.eventId ?? null,
    overTarget: !!tl && overTarget(tl, t), pitchLeft: tl ? pitchLeft(tl, t) : null, autoPitchIn: AUTO_PITCH - t, storyTarget: STORY_TARGET,
    checkpoint: CHECKPOINTS.indexOf(cue.id), checkpoints: CHECKPOINTS.length, log: snap?.log ?? ([] as LogRow[]),
  }
}
export type GuidedDemo = ReturnType<typeof useGuidedDemo>
