// The 「▶ Demo（6 分鐘）」 run in the 3D factory (CJ, 2026-10-22: 「demo按鍵按下去就只能按照流程跑幫我固定視角」,
// 「固定視角的意思是幫我安排連貫的視角」, 「走動按鍵關鍵處可以讓我按按鍵再繼續」). The timeline and the sim pausing are
// the shared driver (demo/driver.ts, the same one the war room uses); this hook adds the 3D's part: the one-take camera
// (demoCamera.ts) on each beat's shot, the robot's little show at the robot beat, the chime, the pitch slides, the
// exit confirm. While it runs, the camera is locked: only continue / pause / exit / slides (and the phone) work.
// Keys (FactoryApp, and the same actions on the Xbox pad, gamepad.ts): G start · Space / N continue · B pause ·
// Esc exit (confirm) · ← → / PageUp PageDown slides · P phone.
import { useCallback, useEffect, useRef, useState } from 'react'
import { DemoFollow, DemoRun, type LogRow } from '../demo/driver'
import { AUTO_FINAL, autoActive } from '../demo/auto'
import { isOrderTask } from '../demo/signals'
import { beatOf, setDemoBeat, type DemoBeat } from '../demo/beat'
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
function monitorFor(cueId: string, eventId: string | null, auto = false, orderId: string | null = null): string | null {
  // auto-play (CJ 10-22: 「是在主控台跑訂單任務跟核准」「絕對不要切到別的頁面」): always the 主控台, with the order task
  // selected once it is in; the 主控台 again at the end
  if (auto) return cueId === 'rush' ? (orderId ? `#approve?event=${encodeURIComponent(orderId)}` : '#agent') : cueId === 'agent' ? '#agent' : null
  if (cueId === 'rush' || cueId === 'console') return '#agent'
  if (cueId === 'agent') return eventId ? `#console?event=${encodeURIComponent(eventId)}` : '#console'
  return null
}

interface Snap { tl: DemoRun['tl']; t: number; sig: DemoRun['latched']; eventId: string | null; fallback: boolean; log: LogRow[]; autoStatus: string; beat: DemoBeat | null }
const snapOf = (r: DemoRun | DemoFollow, beat: DemoBeat | null = null): Snap => ({ tl: r.tl, t: r.t, sig: r.arrived === undefined ? r.latched : { ...r.latched, arrived: r.arrived }, eventId: r.seen.id, fallback: r.fallback, log: [...r.log], autoStatus: r instanceof DemoRun ? (r.autoStatus ?? '') : '', beat })

export function useGuidedDemo({ state, transport, onBegin, onEnd, onWalk, onPhone, nSlides }: {
  state: WarRoomState; transport: Transport | null
  /** the overview hands over to the first-person walk (FactoryApp: first-person view) */
  onWalk: () => void
  /** the demo starts: leave first person, close panels */
  onBegin: () => void
  /** `atDesk`: the auto demo ended at the war-room computer (open the desk monitor on the 主控台) */
  onEnd: (atDesk?: boolean) => void
  /** auto-play: the supervisor's phone comes up / goes down */
  onPhone?: (up: boolean) => void
  nSlides: number
}) {
  /** this screen's driver (a demo started here), or a follower (started on the war room) */
  const run = useRef<DemoRun | DemoFollow | null>(null)
  const [following, setFollowing] = useState(false)
  const [running, setRunning] = useState(false)
  /** what the overlay shows, copied out of the driver every tick */
  const [snap, setSnap] = useState<Snap | null>(null)
  const [slide, setSlide] = useState(0)
  const [slideView, setSlideView] = useState(false)
  const [bigUntil, setBigUntil] = useState(0)
  const [note, setNote] = useState<string | null>(null)
  const [confirmExit, setConfirmExit] = useState(false)
  const [paused, setPaused] = useState(false)
  /** auto-play run (?mock=1&demo=cure07), set at begin() */
  const [auto, setAuto] = useState(false)
  const autoStarted = useRef(false)
  const phoneShown = useRef(false)
  const phoneUpRef = useRef(false)
  /** auto-play: what the timeline last wanted the phone to be (up at 核准, down for the repair); the presenter's P still works */
  const phoneWant = useRef(false)
  const onPhoneRef = useRef(onPhone)
  useEffect(() => { onPhoneRef.current = onPhone })
  const lastRefused = useRef(0)
  const walk = useRef<{ station: StationId | null; monitorAt: number; enteredAt: number }>({ station: null, monitorAt: 0, enteredAt: 0 })
  const [monitor, setMonitor] = useState<string | null>(null)
  const monitorRef = useRef<string | null>(null)
  const slideOpened = useRef(false)
  const stateRef = useRef(state)
  useEffect(() => { stateRef.current = state }, [state])

  /** the demo this screen just left (its started_at): not re-joined while the server's push catches up */
  const left = useRef<string | null>(null)
  const stop = useCallback((atDesk = false) => {
    left.current = stateRef.current.demo?.started_at ?? null
    run.current = null
    setSnap(null)
    setMonitor(null); monitorRef.current = null
    walker.reset()
    if (phoneUpRef.current) { phoneUpRef.current = false; onPhoneRef.current?.(false) }
    phoneWant.current = false
    setDemoBeat(null)
    setAuto(false)
    setRunning(false); setSlideView(false); setConfirmExit(false); setPaused(false)
    director.on = false
    director.clearRadius = 0
    setFollowing(false)
    onEnd(atDesk === true)   // (a click handler passes its event: only a real true counts)
  }, [onEnd])

  /** The 3D's own setup for a demo, whoever drives it. */
  const setup = useCallback(() => {
    onBegin()
    chime()
    demoCam.reset()
    demoCam.reduced = typeof matchMedia !== 'undefined' && matchMedia('(prefers-reduced-motion: reduce)').matches
    walker.reset()
    // the lead starts inside the war-room door, like a fresh start of the 3D (the walk goes from there)
    teleport(START.x, START.z, START.yaw)
    director.on = true
    director.mode = 'orbit'
    walk.current = { station: null, monitorAt: 0, enteredAt: 0 }
    phoneUpRef.current = false; phoneShown.current = false; phoneWant.current = false
    setMonitor(null)
    setSlide(0); setSlideView(false); setBigUntil(0); setConfirmExit(false); setPaused(false); setNote(null)
  }, [onBegin])

  const begin = useCallback(async () => {
    if (!transport) return
    if (run.current) await run.current.exit()
    setup()
    const isAuto = autoActive(transport.mode)
    walker.calm = isAuto; demoCam.calm = isAuto
    const r = new DemoRun(transport, '3d', isAuto)
    run.current = r
    setAuto(isAuto)
    setFollowing(false)
    setRunning(true)
    setSnap(snapOf(r))
    const n = await r.start(performance.now())
    if (n) setNote(n)
  }, [transport, setup])

  // auto-play: start by itself ~800ms after the page is ready, once (never re-opened after it ends or is left)
  const beginRef = useRef(begin)
  useEffect(() => { beginRef.current = begin })
  useEffect(() => {
    if (!transport || !autoActive(transport.mode) || autoStarted.current) return
    const id = setTimeout(() => {
      if (autoStarted.current) return
      autoStarted.current = true
      void beginRef.current()
    }, 800)
    return () => clearTimeout(id)
  }, [transport])

  // started on another screen (the war room's 「▶ Demo 模式」): join and follow it, camera and walk included
  const demoState = state.demo
  useEffect(() => {
    if (!transport || run.current || !demoState?.running || demoState.started_at === left.current) return
    setup()
    run.current = new DemoFollow(transport)
    setFollowing(true)
    setRunning(true)
  }, [transport, demoState?.running, demoState?.started_at, setup])

  /** A / Space / N. At the approval checkpoint it is refused: the approval has to happen on the phone. */
  const cont = useCallback(async () => {
    const r = run.current
    if (!r || paused) return
    if (r instanceof DemoRun && r.auto) { r.skip(); return }   // auto-play: 「下一階段 ▶」 skips to the next beat
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
      const isAuto = r instanceof DemoRun && r.auto
      if (out === 'cue') {
        console.info(`[demo] ${r.t.toFixed(1)} ${c.id} (${r.tl.fired[r.tl.fired.length - 1].reason})`)
        if (c.big) { chime(true); setBigUntil(now + 2500) } else chime()
        if (c.id === 'pitch') setSlide(0)
        w.enteredAt = now
        w.station = stationFor(c.id, c.shot)
        // auto-play: the 7 號機 alarm and the phone are seen from the lookout behind the desk (first person, no walk)
        if (isAuto && (c.id === 'event' || c.id === 'h1')) w.station = 'lookout'
        // the robot comes over to the lead at 7 號機, then plays its show facing the camera
        // the robot heads over while the crew repairs 7 號機, so at its beat it is already there for the show
        if (c.id === 'worker') robot.visitReq = ROBOT_MEET
        if (c.id === 'robot') { if (robot.playing == null) robot.playReq = true }
      }
      // camera: the opening overview (orbit), a hand-over onto the lead, then the first-person walk between stations
      // (auto-play walks the same stations: overview → desk (orders) → doorway (7 號機) → press 7 → robot → desk)
      if (director.mode === 'orbit') {
        if (c.shot.kind === 'establish') demoCam.setShot(c.shot, null)
        else {
          demoCam.setShot({ kind: 'lead' } as unknown as Shot, null)
          if (!demoCam.flying && now - w.enteredAt > 300) { director.mode = 'walk'; onWalk() }
        }
      }
      // auto-play: the order is done (the 急單 hold is over) → the lead leaves the computer and walks out to 7 號機; the
      // anomaly comes in on the way (CJ 10-22: 「訂單結束之後我要離開電腦然後走向工廠，然後手機才響」)
      if (isAuto && c.id === 'rush' && r.tl.released && w.station === 'desk') w.station = 'lookout'
      if (director.mode === 'walk' && w.station && walker.at !== w.station) walker.go(w.station, { run: c.id === 'worker' })
      // the war-room computer: open at the desk once the lead has arrived (主控台 for the orders, Agent page later)
      // the order task the demo started (the newest 今日訂單排程 / order event): selected on the 主控台
      const orderId = isAuto ? [...(stateRef.current.snapshot?.events ?? [])].filter((e) => isOrderTask(e.id))
        .sort((a, b) => (a.opened_at < b.opened_at ? 1 : -1))[0]?.id ?? null : null
      const want = monitorFor(c.id, r.seen.id, isAuto, orderId)
      const atDesk = director.mode === 'walk' && walker.at === 'desk' && walker.done
      const show = want && atDesk ? want : null
      if (show !== monitorRef.current) { monitorRef.current = show; setMonitor(show); w.monitorAt = now }
      // the pitch: standing at the board, continue opens the slides full screen
      if (c.id === 'pitch' && r.tl.released && !slideOpened.current) { slideOpened.current = true; setSlideView(true) }
      if (c.id !== 'pitch' && c.id !== 'warn' && c.id !== 'end') slideOpened.current = false
      // a checkpoint is ready only once the walk (and the monitor, the robot's show) has arrived
      const robotDone = c.id !== 'robot' || (robot.played.includes('bow') && robot.playing == null) || now - w.enteredAt > 12000
      const monitorDone = !want || (!!show && now - w.monitorAt > 300)
      r.arrived = director.mode === 'walk' ? walker.done && robotDone && monitorDone : c.shot.kind === 'establish' ? undefined : false
      // the doorway view of 7 號機: nobody walks through the foreground between the camera and the press
      director.clearRadius = director.mode === 'walk' && (walker.at === 'doorway' || walker.at === 'lookout') && walker.done ? 3.2 : 0
      // auto-play: the supervisor's phone rings (FactoryApp: buzz + ring) and comes up only at the phone beat (h1), after
      // the lead has walked out and looked at 7 號機; it stays up for the approval (the real backend: someone taps it),
      // then goes down so the repair can be seen. The rush order is resolved on the war-room computer, not the phone.
      if (isAuto && r instanceof DemoRun) {
        const up = c.id === 'h1'
        if (up !== phoneWant.current) {
          phoneWant.current = up
          if (up !== phoneUpRef.current) { phoneUpRef.current = up; phoneShown.current ||= up; onPhoneRef.current?.(up) }
        }
      }
      // the beat the prompter reads (demo/beat.ts): walk until the lead stands at the war-room computer, then orders,
      // then leave once the order's hold is over
      const beat = isAuto ? beatOf(c.id, director.mode === 'walk' && walker.at === 'desk' && walker.done, r.tl.released) : null
      setDemoBeat(beat)
      if (!isAuto && r.refusedAt > lastRefused.current) {
        lastRefused.current = r.refusedAt
        const w = waiting(r.tl, r.t, r.latched)
        setNote(w === 'approval' ? '要先核准（手機或核准卡），才能往下' : r.arrived === false ? '還在走過去，到了再按' : '等工廠下一件事發生，不能跳過')
      }
      // auto-play's end (CJ: 「這邊開始就不用自動了」): back at the desk with the 主控台 up → finish (1×, faults cleared,
      // random events off) and leave the demo: no pitch, no more cues, the lead sits at the war-room computer
      if (isAuto && r instanceof DemoRun && c.id === AUTO_FINAL && r.arrived && !r.finished) {
        void r.finish().then(() => r.exit()).finally(() => stop(true))
        return
      }
      const d = stateRef.current.demo
      if (d?.running && d.paused !== paused) setPaused(d.paused)
      setSnap(snapOf(r, beat))
    }, 150)
    return () => clearInterval(id)
  }, [running, stop, paused, onWalk])
  useEffect(() => () => { director.on = false; setDemoBeat(null) }, [])

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
        running: true, t: x.t, cue: current(x.tl).id, beat: snap?.beat ?? null, auto: x instanceof DemoRun && x.auto,
        autoApprove: x instanceof DemoRun && x.autoApprove, autoStatus: x instanceof DemoRun ? x.autoStatus : '', index: x.tl.index, released: x.tl.released,
        waiting: waiting(x.tl, x.t, x.arrived === undefined ? x.latched : { ...x.latched, arrived: x.arrived }),
        simHeld: x.simHeld, paused, slide, slideView, fallback: x.fallback, log: x.log, signals: x.firstSeen, seen: { id: x.seen.id, machine: x.seen.machine },
        walk: { mode: director.mode, at: walker.at, done: walker.done }, monitor: monitorRef.current, arrived: x.arrived,
        camMaxStep: demoCam.maxStep, flying: demoCam.flying, robotPlayed: [...robot.played], camTraceLen: demoCam.trace.length,
      } : { running: false, director: director.on }
    }
    w2.__demoCamTrace = () => demoCam.trace
    return () => { delete w2.__demo3d; delete w2.__demoCamTrace }
  }, [paused, slide, slideView, snap?.beat])

  return {
    running, t, cue, monitor, following,
    /** the pitch board is the subject: world labels and the HUD stay out of the way */
    quiet: running && (cue.id === 'pitch' || cue.id === 'warn' || cue.id === 'end'), next: tl ? upcoming(tl) : null, waiting: w, ready: isReady, released: !!tl?.released,
    auto, autoStatus: snap?.autoStatus ?? '', beat: snap?.beat ?? null,
    paused, slide, setSlide, changeSlide, slideView, setSlideView, bigUntil, note, setNote, confirmExit, setConfirmExit,
    begin, stop, exit, cont, togglePause, fallback: snap?.fallback ?? false, incidentEventId: snap?.eventId ?? null,
    overTarget: !!tl && overTarget(tl, t), pitchLeft: tl ? pitchLeft(tl, t) : null, autoPitchIn: AUTO_PITCH - t, storyTarget: STORY_TARGET,
    checkpoint: CHECKPOINTS.indexOf(cue.id), checkpoints: CHECKPOINTS.length, log: snap?.log ?? ([] as LogRow[]),
  }
}
export type GuidedDemo = ReturnType<typeof useGuidedDemo>
