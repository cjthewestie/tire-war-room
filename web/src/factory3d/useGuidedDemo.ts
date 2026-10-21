// The 「▶ Demo（6 分鐘）」 run in the 3D factory (CJ, 2026-10-22: 「demo按鍵按下去就只能按照流程跑幫我固定視角」,
// 「固定視角的意思是幫我安排連貫的視角」, 「走動按鍵關鍵處可以讓我按按鍵再繼續」). The timeline and the sim pausing are
// the shared driver (demo/driver.ts, the same one the war room uses); this hook adds the 3D's part: the one-take camera
// (demoCamera.ts) on each beat's shot, the robot's little show at the robot beat, the chime, the pitch slides, the
// exit confirm. While it runs, the camera is locked: only continue / pause / exit / slides (and the phone) work.
// Keys (FactoryApp, and the same actions on the Xbox pad, gamepad.ts): G start · Space / N continue · B pause ·
// Esc exit (confirm) · ← → / PageUp PageDown slides · P phone.
import { useCallback, useEffect, useRef, useState } from 'react'
import { DemoRun, type LogRow } from '../demo/driver'
import { AUTO_PITCH, CHECKPOINTS, CUES, STORY_TARGET, current, overTarget, pitchLeft, ready, upcoming, waiting } from '../demo/cues'
import type { WarRoomState } from '../store'
import type { Transport } from '../transport'
import { demoCam } from './demoCamera'
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

interface Snap { tl: DemoRun['tl']; t: number; sig: DemoRun['latched']; eventId: string | null; fallback: boolean; log: LogRow[] }
const snapOf = (r: DemoRun): Snap => ({ tl: r.tl, t: r.t, sig: r.latched, eventId: r.seen.id, fallback: r.fallback, log: [...r.log] })

export function useGuidedDemo({ state, transport, onBegin, onEnd, nSlides }: {
  state: WarRoomState; transport: Transport | null
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
  const stateRef = useRef(state)
  useEffect(() => { stateRef.current = state }, [state])

  const stop = useCallback(() => {
    run.current = null
    setSnap(null)
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
    director.on = true
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
      demoCam.setShot(c.shot, r.seen.machine ?? robot.target)
      if (out === 'cue') {
        console.info(`[demo] ${r.t.toFixed(1)} ${c.id} (${r.tl.fired[r.tl.fired.length - 1].reason})`)
        if (c.big) { chime(true); setBigUntil(performance.now() + 4000) } else chime()
        if (c.id === 'robot') robot.playReq = true
        if (c.id === 'pitch') setSlide(0)
      }
      if (r.refusedAt > lastRefused.current) {
        lastRefused.current = r.refusedAt
        const w = waiting(r.tl, r.t, r.latched)
        setNote(w === 'approval' ? '要先核准（手機或核准卡），才能往下' : '等工廠下一件事發生，不能跳過')
      }
      const d = stateRef.current.demo
      if (d?.running && d.paused !== paused) setPaused(d.paused)
      setSnap(snapOf(r))
    }, 250)
    return () => clearInterval(id)
  }, [running, stop, paused])
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
        running: true, t: x.t, cue: current(x.tl).id, index: x.tl.index, released: x.tl.released, waiting: waiting(x.tl, x.t, x.latched),
        simHeld: x.simHeld, paused, slide, slideView, fallback: x.fallback, log: x.log, signals: x.firstSeen, seen: { id: x.seen.id, machine: x.seen.machine },
        camMaxStep: demoCam.maxStep, flying: demoCam.flying, robotPlayed: [...robot.played],
      } : { running: false, director: director.on }
    }
    return () => { delete w2.__demo3d }
  }, [paused, slide, slideView])

  return {
    running, t, cue, next: tl ? upcoming(tl) : null, waiting: w, ready: isReady, released: !!tl?.released,
    paused, slide, setSlide, changeSlide, slideView, setSlideView, bigUntil, note, setNote, confirmExit, setConfirmExit,
    begin, stop, exit, cont, togglePause, fallback: snap?.fallback ?? false, incidentEventId: snap?.eventId ?? null,
    overTarget: !!tl && overTarget(tl, t), pitchLeft: tl ? pitchLeft(tl, t) : null, autoPitchIn: AUTO_PITCH - t, storyTarget: STORY_TARGET,
    checkpoint: CHECKPOINTS.indexOf(cue.id), checkpoints: CHECKPOINTS.length, log: snap?.log ?? ([] as LogRow[]),
  }
}
export type GuidedDemo = ReturnType<typeof useGuidedDemo>
