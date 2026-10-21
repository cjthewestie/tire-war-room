// 3D factory (served at /factory). You are the war-room lead, in first person, inside a Minimotive-style miniature
// factory building: walk the rows (F at a machine for its live readings), sit at the war-room computer (F at the desk)
// to use the real dashboard, watch it live on the big wall screen, go through the doors to the 休息區 and 廁所
// (separate spaces), and raise the real /phone page (P) wherever you are. V switches to the diorama overview.
// F at a machine with an anomaly also shows its SOP 處置步驟 to tick off and report done (#72, FieldSop).
// Presenter keys (#25): 1 loads the 7 號硫化機溫降 scenario, 5 the 3 件同時異常 one, 6 正常運轉 (the demo strip's
// default three), 2 / 3 run at 10× / 60×, 0 pauses or plays, so the demo never has to leave the 3D view; ?demo=1 also
// lists them in the key strip and turns key 4 (隨機事件) off. #71: the ⚡ 觸發異常 button (or T) does the same with a
// click, for presenters and touch devices. 情境疊加: 5 (or a menu scenario) while another scenario's event or approval
// is open adds it on top instead of reloading (components/scenarioOverlay.ts); the note says what was added.
// ▶ Demo（6 分鐘） (G, or Menu on the pad): the guided run for the finals (useGuidedDemo.ts, demo/cues.ts): presenter
// checkpoints (Space / N / A continue, the sim waits meanwhile), one continuous camera take (demoCamera.ts), the robot's
// beat, and the pitch on the whiteboard (Whiteboard.tsx; CJ's slides in web/public/pitch/). The camera is locked while
// it runs: only continue, pause (B / Menu), exit (Esc / hold B), slides (← → / LB RB) and the phone (P / X) work.
// Xbox controller (gamepad.ts): sticks walk and look, D-pad steps through the machines, A opens, B backs out, X phone,
// Y flies to an abnormal machine (else the whiteboard), View held shows the mapping.
// Phones and tablets (#26) start in the overview, with a stick and buttons on screen (TouchControls).
// All state comes from the same WarRoomProvider as the dashboard (live /ws, or the mock engine).
import { Canvas } from '@react-three/fiber'
import * as THREE from 'three'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { DEMO_MODE } from '../components/DemoStrip'
import { MachineDrawer } from '../components/MachineDrawer'
import { OVERLAY_NOTE_MS, overlayNote, scenarioClick } from '../components/scenarioOverlay'
import type { SimControl } from '../contracts'
import { STATUS_LABEL } from '../components/common'
import { machineLabel } from '../factory'
import { fmtHM, fmtMD } from '../format'
import { useLinkStatus } from '../linkStatus'
import { alertText, diffPending, reviewAlerts, type PushState } from '../phone/alert'
import { SCENARIOS } from '../mock/engine'
import { primaryEvent } from '../store'
import { useWarRoom } from '../useWarRoom'
import { AnomalyMenu } from './AnomalyMenu'
import { DeskMonitor } from './DeskMonitor'
import { FieldSop } from './FieldSop'
import { DemoOverlay, PadHelp, SlideView } from './GuidedDemo'
import { alarms, arrowEls } from './alarm'
import { BTN, approach, usePad, type Btn, type PadFrame } from './gamepad'
import { demoKey, padActions, type Action } from './demoInput'
import { useGuidedDemo } from './useGuidedDemo'
import { useDeck } from './pitchSlides'
import { AiStatusLine } from '../AiStatusLine'
import { successTitle, useSuccessMoments } from './success'
import { PLACED, PLACED_BY_ID, ROOMS, WHITEBOARD, collide, walkable, type Near, type SceneId } from './layout'
import { releaseLook, requestLook } from './look'
import { Player } from './Player'
import { PhoneOverlay } from './PhoneOverlay'
import { CAM_LIMITS, TOUCH_DEVICE, cam, player, stick, teleport, view } from './pose'
import { BreakRoom, Restroom } from './Rooms'
import { SPACES, arrival } from './spaces'
import { TiltShift } from './TiltShift'
import { TouchControls } from './TouchControls'
import { DRAWN_LIGHT, Lights, World } from './World'

type Panel = { kind: 'machine'; id: string } | { kind: 'monitor' } | null
const SCENE_LABEL: Record<SceneId, string> = { factory: '工廠', break: '休息區', toilet: '廁所' }
const PRESS7 = (SCENARIOS.find((s) => s.name === 'press7_temp_drop')?.label ?? '7 號硫化機溫降').replace(/（.*?）/, '')
const CONC3 = SCENARIOS.find((s) => s.name === 'concurrent_3') ?? { label: '3 件同時異常', speed: 5 }
const NORMAL = SCENARIOS.find((s) => s.name === 'normal') ?? { label: '正常運轉', speed: 1 }
/** Presenter keys → the same /api/sim/control calls as the dashboard's demo strip. Digit0 toggles, so it is built per press. */
const DEMO_KEYS: Record<string, (paused: boolean) => { c: SimControl; label: string }> = {
  Digit1: () => ({ c: { action: 'scenario', scenario: 'press7_temp_drop', speed: 10, preroll: true }, label: `載入情境：${PRESS7}` }),
  Digit5: () => ({ c: { action: 'scenario', scenario: 'concurrent_3', speed: CONC3.speed, preroll: true }, label: `載入情境：${CONC3.label}` }),
  Digit6: () => ({ c: { action: 'scenario', scenario: 'normal', speed: NORMAL.speed, preroll: true }, label: `載入情境：${NORMAL.label}` }),
  Digit2: () => ({ c: { action: 'speed', speed: 10 }, label: '10 倍速' }),
  Digit3: () => ({ c: { action: 'speed', speed: 60 }, label: '60 倍速' }),
  Digit0: (paused) => ({ c: { action: paused ? 'play' : 'pause' }, label: paused ? '繼續播放' : '暫停' }),
}

/** The HUD's 已暫停 chip, in the amber tone of the link chip (.f3-conn.t-warn). */
const PAUSED_CHIP = { padding: '0 9px', borderRadius: 999, background: '#fbecd0', color: 'var(--c-amber)', fontSize: 13, fontWeight: 700 } as const

let audio: AudioContext | null = null
function unlockAudio() {
  try {
    if (!audio && typeof AudioContext !== 'undefined') audio = new AudioContext()
    if (audio?.state === 'suspended') void audio.resume().catch(() => {})
  } catch { /* no audio */ }
}
function ring() {
  try {
    if (!audio || audio.state !== 'running') return
    const t0 = audio.currentTime
    ;[0, 0.18, 0.36].forEach((dt) => {
      const o = audio!.createOscillator()
      const g = audio!.createGain()
      o.frequency.value = 988
      o.connect(g); g.connect(audio!.destination)
      g.gain.setValueAtTime(0.0001, t0 + dt)
      g.gain.exponentialRampToValueAtTime(0.3, t0 + dt + 0.02)
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + dt + 0.14)
      o.start(t0 + dt); o.stop(t0 + dt + 0.15)
    })
  } catch { /* ignore */ }
}

/** In front of a machine, in the aisle, facing it. First person stands a step further back and looks up a little,
 *  so the status lamp on its post and the reading tag above it are in view. */
function standAt(id: string) {
  const p = PLACED_BY_ID[id]
  if (!p) return
  const side = p.line === 'CURE' ? -1 : 1   // presses: stand on the aisle north of the row
  const first = view.mode === 'first'
  const z = p.z + side * (p.d / 2 + (first ? 1.2 : 0.45))
  const yaw = side > 0 ? 0 : Math.PI
  teleport(p.x, z, yaw, yaw + 0.6)
  if (first) player.pitch = 0.22
}

function promptText(n: Near) {
  if (!n) return null
  if (n.kind === 'desk') return '坐下使用戰情室電腦'
  if (n.kind === 'door') return n.to === 'factory' ? '回到工廠' : `進入${n.label}`
  return `查看 ${machineLabel(n.id)}`
}

export function FactoryApp() {
  const { state, transport, connected } = useWarRoom()
  const link = useLinkStatus()
  const [started, setStarted] = useState(false)
  const [near, setNear] = useState<Near>(null)
  const [panel, setPanel] = useState<Panel>(null)
  const [phoneUp, setPhoneUp] = useState(false)
  const [mode, setMode] = useState<'first' | 'third'>(TOUCH_DEVICE ? 'third' : 'first')
  const [scene, setScene] = useState<SceneId>('factory')
  const [fading, setFading] = useState(false)
  const [locked, setLocked] = useState(false)
  const [toast, setToast] = useState<{ title: string; body: string; at: number; review?: boolean } | null>(null)
  const [buzzUntil, setBuzzUntil] = useState(0)
  const [buzz, setBuzz] = useState(false)
  const [demoNote, setDemoNote] = useState<{ text: string; at: number; ms?: number } | null>(null)
  const [anomOpen, setAnomOpen] = useState(false)      // #71 觸發異常 menu
  const success = useSuccessMoments(state.world)         // issue #36: 訂單完成 / 出貨已出廠
  const [shown, setShown] = useState<string | null>(null)     // the success moment whose 5 s are over
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const cameraRef = useRef<THREE.Camera | null>(null)
  const nearRef = useRef<Near>(null)
  useEffect(() => { nearRef.current = near }, [near])
  const fadingRef = useRef(false)
  const phoneUpRef = useRef(false)
  const push = useRef<PushState>({ seen: new Set(), held: [] })

  const ev = primaryEvent(state.snapshot?.events)
  const evOpen = ev && ev.state !== 'closed' && ev.state !== 'safe_stop'
  const pending = state.pending
  // fast repair: repaired events waiting for the supervisor's 覆核 ring the phone like a new approval
  const reviews = useMemo(() => reviewAlerts(state.snapshot?.events), [state.snapshot?.events])
  const ft = state.clock?.factory_time ?? state.snapshot?.header.factory_time
  const overlay = panel !== null || phoneUp
  const lanDown = state.snapshot?.header.network === 'lan_down'
  const fp = started && mode === 'first'
  const paused = !!state.clock?.paused

  // New approval (or 覆核 request) → phone buzz + ring + HUD toast (same diff the phone page uses), in every space.
  useEffect(() => {
    const out = diffPending(push.current, [...pending, ...reviews], connected && !lanDown)
    if (!out.fire.length) return
    const { title, body } = alertText(out.fire, out.flushed)
    setToast({ title, body, at: Date.now(), review: out.fire.every((a) => a.review) })
    setBuzzUntil(performance.now() + 2200)
    setBuzz(true)
    try { navigator.vibrate?.([200, 100, 200]) } catch { /* unsupported */ }
    ring()
  }, [pending, reviews, connected, lanDown])
  useEffect(() => {
    if (!buzz) return
    const t = setTimeout(() => setBuzz(false), 2200)
    return () => clearTimeout(t)
  }, [buzz, buzzUntil])
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 6000)
    return () => clearTimeout(t)
  }, [toast])

  useEffect(() => {
    const m = success.latest
    if (!m) return
    const t = setTimeout(() => setShown(m.key), 5000)
    return () => clearTimeout(t)
  }, [success.latest])
  // test hook (headless e2e only: dev build or ?test=1)
  useEffect(() => {
    if (!import.meta.env.DEV && !new URLSearchParams(location.search).has('test')) return
    const w = window as unknown as Record<string, unknown>
    w.__success3d = () => success.all.map((m) => ({ kind: m.kind, title: successTitle(m) }))
    return () => { delete w.__success3d }
  }, [success.all])

  useEffect(() => {
    if (!demoNote) return
    const t = setTimeout(() => setDemoNote(null), demoNote.ms ?? 2500)
    return () => clearTimeout(t)
  }, [demoNote])
  // ▶ Demo（6 分鐘）: the camera is the director's while it runs, so the lead stands still and the overview is on
  const deck = useDeck()
  const onDemoBegin = useCallback(() => {
    setStarted(true); setMode('third'); setPanel(null); setPhoneUp(false); setAnomOpen(false); releaseLook()
  }, [])
  const onDemoEnd = useCallback(() => { window.focus(); canvasRef.current?.focus() }, [])
  const nSlides = deck.slides.length
  const demo = useGuidedDemo({ state, transport, onBegin: onDemoBegin, onEnd: onDemoEnd, nSlides })
  const pitchView = demo.running && demo.cue.shot.kind === 'whiteboard'
  const { running: demoOn, setSlide, setSlideView, slideView, changeSlide } = demo
  // demo keys (demoInput.ts): while the demo runs these are the only keys that work
  const runAction = useCallback((act: Action) => {
    if (act === 'start') void demo.begin()
    else if (act === 'continue') void demo.cont()
    else if (act === 'pause') demo.togglePause()
    else if (act === 'exit-ask') demo.setConfirmExit(true)
    else if (act === 'exit-yes') void demo.exit()
    else if (act === 'exit-no') demo.setConfirmExit(false)
    else if (act === 'slide+') changeSlide(1)
    else if (act === 'slide-') changeSlide(-1)
    else if (act === 'slides-close') setSlideView(false)
  }, [demo, changeSlide, setSlideView])
  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      const tg = e.target as HTMLElement | null
      if (tg && (tg.tagName === 'INPUT' || tg.tagName === 'TEXTAREA') || e.repeat) return
      const act = demoKey(e.code, { demoOn, confirmExit: demo.confirmExit, slides: pitchView || slideView, slideView, phoneUp: phoneUpRef.current })
      if (!act || act === 'phone') return   // the phone (P) stays with the main handler
      if (act === 'continue') e.preventDefault()
      runAction(act)
    }
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [demo.confirmExit, demoOn, pitchView, slideView, runAction])

  const stateRef = useRef(state)          // 情境疊加: the key handler decides overlay / load on the latest state
  useEffect(() => { stateRef.current = state }, [state])

  useEffect(() => {
    const f = () => setLocked(!!document.pointerLockElement)
    document.addEventListener('pointerlockchange', f)
    return () => document.removeEventListener('pointerlockchange', f)
  }, [])

  const start = useCallback(() => {
    unlockAudio()
    setStarted(true)
    requestLook()
    canvasRef.current?.focus()
  }, [])

  const raisePhone = useCallback((up: boolean) => {
    setPhoneUp(up)
    if (up) { setPanel(null); releaseLook() } else { window.focus(); canvasRef.current?.focus() }
  }, [])

  const closePanel = useCallback(() => { setPanel(null); window.focus(); canvasRef.current?.focus() }, [])

  /** Go through a door: a short fade, then the lead stands in the other space (just inside its door). */
  const goTo = useCallback((to: SceneId) => {
    if (fadingRef.current || to === view.scene) return
    fadingRef.current = true
    setFading(true)
    setTimeout(() => {
      const a = arrival(to, view.scene)
      view.scene = to
      setScene(to)
      setNear(null)
      teleport(a.x, a.z, a.yaw)
      setTimeout(() => { setFading(false); fadingRef.current = false }, 120)
    }, 320)
  }, [])

  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA')) return
      if (!started && (e.code === 'Enter' || e.code === 'Space')) { start(); return }
      unlockAudio()
      // the demo runs the flow: no free keys, only the phone (approving is part of the story)
      if (demoOn) { if (e.code === 'KeyP') raisePhone(!phoneUp); else if (e.code === 'Escape' && phoneUp) raisePhone(false); return }
      const demo = DEMO_KEYS[e.code]
      if (demo) {
        if (!transport || e.repeat) return
        const { c, label } = demo(paused)
        // 情境疊加: 1 / 5 while an event or approval is open on another scenario add on top (the demo strip's rule)
        if (c.action === 'scenario' && c.scenario && scenarioClick(c.scenario, stateRef.current) === 'overlay') {
          const name = c.scenario
          const lab = SCENARIOS.find((s) => s.name === name)?.label ?? name
          transport.overlay(name).then((r) => setDemoNote({ text: overlayNote(lab, r), at: Date.now(), ms: OVERLAY_NOTE_MS }),
            (err) => { console.warn(err); setDemoNote({ text: `疊加：${lab}：沒有成功`, at: Date.now() }) })
          return
        }
        // 1 / 5 while paused used to load the scenario and stay paused (the plant looked frozen): loading also plays
        const sent = c.action === 'scenario' ? transport.control(c).then(() => transport.control({ action: 'play' })) : transport.control(c)
        sent.then(() => setDemoNote({ text: label, at: Date.now() }), (err) => { console.warn(err); setDemoNote({ text: `${label}：沒有成功`, at: Date.now() }) })
        return
      }
      if (e.code === 'Digit4' && !DEMO_MODE) {   // issue #36: 隨機事件 on / off (the same switch as the demo strip)
        if (!transport?.setSimEvents || e.repeat) return
        const on = !state.clock?.random_events?.enabled
        transport.setSimEvents({ enabled: on }).then(
          (v) => setDemoNote({ text: v.enabled ? `隨機事件：開（每 ${v.every_min} 分）` : '隨機事件：關', at: Date.now() }),
          (err) => { console.warn(err); setDemoNote({ text: '隨機事件：沒有成功', at: Date.now() }) })
        return
      }
      if (e.code === 'KeyT' && !e.repeat) { if (!anomOpen) releaseLook(); setAnomOpen(!anomOpen); return }
      if (e.code === 'KeyP') { raisePhone(!phoneUp); return }
      if (e.code === 'Escape') { if (anomOpen) setAnomOpen(false); else if (phoneUp) raisePhone(false); else if (panel) closePanel(); return }
      if (e.code === 'KeyV' && started && !overlay) { setMode((m) => (m === 'first' ? 'third' : 'first')); return }
      if (e.code === 'KeyF') {
        if (panel) { closePanel(); return }
        if (phoneUp || !started) return
        const n = nearRef.current
        if (!n) return
        if (n.kind === 'door') { goTo(n.to); return }
        releaseLook()
        setPanel(n.kind === 'desk' ? { kind: 'monitor' } : { kind: 'machine', id: n.id })
      }
    }
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [started, phoneUp, panel, overlay, start, raisePhone, closePanel, goTo, transport, paused, state.clock?.random_events?.enabled, anomOpen, demoOn])

  // Test hook (headless e2e moves the player without a mouse). Only in dev or with ?test=1,
  // so a normal viewer's bundle does not expose teleport.
  useEffect(() => {
    if (!import.meta.env.DEV && !new URLSearchParams(location.search).has('test')) return
    const w = window as unknown as Record<string, unknown>
    // screenOf: where a world point lands on screen (CSS px), for layout asserts; toast: show the approval toast
    const screenOf = (x: number, y: number, z: number) => {
      const c = cameraRef.current, el = canvasRef.current
      if (!c || !el) return null
      const v = new THREE.Vector3(x, y, z).project(c)
      if (v.z > 1) return null   // behind the camera
      const r = el.getBoundingClientRect()
      return { x: r.left + (v.x + 1) / 2 * r.width, y: r.top + (1 - v.y) / 2 * r.height }
    }
    const toastNow = (title = '測試通知', body = '7 號硫化機：待核准 1 件') => setToast({ title, body, at: Date.now() })
    w.__factory3d = {
      teleport, standAt, player, cam, view, walkable, collide, raisePhone, screenOf, toast: toastNow, placed: PLACED_BY_ID, rooms: ROOMS,
      goTo, spaces: SPACES, lights: DRAWN_LIGHT, cameraPos: () => cameraRef.current?.position.toArray(),
      fov: () => (cameraRef.current as THREE.PerspectiveCamera | null)?.fov,
    }
    return () => { delete w.__factory3d }
  }, [raisePhone, goTo])

  // ---------------------------------------------------------------- warning lights (Alarms.tsx, alarm.ts)
  const alarmMap = useMemo(() => alarms(state.snapshot?.machines, state.snapshot?.events, pending), [state.snapshot?.machines, state.snapshot?.events, pending])
  const alarmIds = Object.keys(alarmMap).filter((id) => PLACED_BY_ID[id])

  // ---------------------------------------------------------------- Xbox controller (gamepad.ts)
  const [padFocus, setPadFocus] = useState<string | null>(null)
  const padIdx = useRef(-1)
  const padLook = useRef({ x: 0, y: 0, fw: 0, st: 0 })
  const bHeldFired = useRef(false)
  const live = useRef({ demo, demoOn, started, panel, phoneUp, anomOpen, slideView, pitchView, tiles: state.snapshot?.machines, alarms: alarmMap, run: runAction })
  useEffect(() => { live.current = { demo, demoOn, started, panel, phoneUp, anomOpen, slideView, pitchView, tiles: state.snapshot?.machines, alarms: alarmMap, run: runAction } })
  useEffect(() => { phoneUpRef.current = phoneUp }, [phoneUp])
  useEffect(() => { if (!padFocus) return; const id = setTimeout(() => setPadFocus(null), 4000); return () => clearTimeout(id) }, [padFocus])
  const pad = usePad(useCallback((f: PadFrame, edges: Btn[], held: Record<number, number>, dt: number) => {
    const L = live.current
    unlockAudio()
    const nearBoard = Math.hypot(player.x - WHITEBOARD.x, player.z - WHITEBOARD.z) < 4.5
    const out = padActions(f, edges, held, {
      demoOn: L.demoOn, confirmExit: L.demo.confirmExit, slides: L.pitchView || L.slideView || nearBoard, slideView: L.slideView, phoneUp: L.phoneUp,
    }, bHeldFired.current)
    if (!(held[BTN.B] > 0)) bHeldFired.current = false
    if (out.actions.includes('exit-ask')) bHeldFired.current = true
    if (!L.started && !L.demoOn) { if (edges.length) { if (out.actions.includes('start')) void L.demo.begin(); else start() } return }
    // walk / look: eased toward the stick, capped (no sudden lurches); zero while the demo holds the camera
    const pl = padLook.current
    pl.fw = approach(pl.fw, out.move[0], dt, 8); pl.st = approach(pl.st, out.move[1], dt, 8)
    pl.x = approach(pl.x, out.look[0], dt, 7); pl.y = approach(pl.y, out.look[1], dt, 7)
    stick.fw = Math.abs(pl.fw) < 0.02 ? 0 : pl.fw
    stick.st = Math.abs(pl.st) < 0.02 ? 0 : pl.st
    if (!L.demoOn && !L.panel && !L.phoneUp) {
      if (view.mode === 'first') {
        player.yaw -= pl.x * 2.2 * dt
        player.pitch = Math.max(-1.1, Math.min(1.1, player.pitch - pl.y * 1.5 * dt))
      } else {
        cam.yaw -= pl.x * 1.8 * dt
        cam.pitch = Math.max(CAM_LIMITS.pitchMin, Math.min(CAM_LIMITS.pitchMax, cam.pitch + pl.y * 0.9 * dt))
        if (out.zoom) cam.dist = Math.max(CAM_LIMITS.distMin, Math.min(CAM_LIMITS.distMax, cam.dist * Math.exp(out.zoom * 1.3 * dt)))
      }
    }
    for (const act of out.actions) {
      if (act === 'phone') raisePhone(!L.phoneUp)
      else if (act === 'focus+' || act === 'focus-' || act === 'row+' || act === 'row-') {
        // D-pad: step through the machines (← → along the row, ↑ ↓ about a row), standing in front of each
        const n = PLACED.length, d = act === 'focus+' ? 1 : act === 'focus-' ? -1 : act === 'row+' ? 7 : -7
        padIdx.current = padIdx.current < 0 ? 0 : (padIdx.current + d + n) % n
        const p = PLACED[padIdx.current]
        standAt(p.id)
        const tile = L.tiles?.find((m) => m.machine_id === p.id)
        setPadFocus(`${machineLabel(p.id)}・${tile ? STATUS_LABEL[tile.status] ?? tile.status : '—'}${tile?.temp_c != null ? `・${tile.temp_c.toFixed(1)}°C` : ''}`)
      } else if (act === 'open') {
        const nr = nearRef.current
        if (L.panel) closePanel()
        else if (nr?.kind === 'door') goTo(nr.to)
        else if (nr) { releaseLook(); setPanel(nr.kind === 'desk' ? { kind: 'monitor' } : { kind: 'machine', id: nr.id }) }
        else if (padIdx.current >= 0) setPanel({ kind: 'machine', id: PLACED[padIdx.current].id })
      } else if (act === 'back') {
        if (L.anomOpen) setAnomOpen(false); else if (L.phoneUp) raisePhone(false); else if (L.panel) closePanel()
      } else if (act === 'fly') {
        // an abnormal machine first (the off-screen arrows point at the same ones), else the whiteboard
        const hot = Object.entries(L.alarms).filter(([id]) => PLACED_BY_ID[id]).map(([id]) => id)
        if (hot.length) { setMode('third'); standAt(hot[0]) } else teleport(WHITEBOARD.x - 2.4, WHITEBOARD.z, -Math.PI / 2, -Math.PI / 2)
      } else L.run(act)
    }
  }, [raisePhone, closePanel, goTo, start]))

  const prompt = started && !overlay && !fading ? promptText(near) : null

  return (
    <div className="f3-root">
      <Canvas
        className="f3-canvas"
        camera={{ fov: 32, near: 0.1, far: 200, position: [0, 20, 20] }}
        dpr={[1, 1.5]}
        shadows="percentage"
        gl={{ antialias: true, powerPreference: 'high-performance', alpha: true }}
        onCreated={({ gl, camera }) => {
          canvasRef.current = gl.domElement
          cameraRef.current = camera
          gl.domElement.tabIndex = 0
          gl.toneMapping = THREE.ACESFilmicToneMapping
          gl.toneMappingExposure = 1.05
        }}
      >
        <color attach="background" args={['#e9dfcf']} />
        <fog attach="fog" args={['#e9dfcf', 46, 110]} />
        <Lights />
        <World state={state} active={scene === 'factory'} fp={fp} celebrate={success.latest?.kind === 'shipment_departed' ? success.latest.at : null} focusId={started && !overlay && near?.kind === 'machine' ? near.id : null}
          board={{ slides: deck.slides, index: demo.slide, onOpen: () => setSlideView(true) }} alarms={alarmMap} arrows={!demoOn && scene === 'factory'} />
        {scene === 'break' && <BreakRoom />}
        {scene === 'toilet' && <Restroom />}
        <Player
          enabled={started && !overlay && !fading && !demoOn} started={started} onNear={setNear} onDoor={goTo} phoneUp={phoneUp}
          pending={pending.length + reviews.length} buzzUntil={buzzUntil} mode={mode} scene={scene}
          prompt={prompt}
        />
        <TiltShift enabled={(!started || mode === 'third') && !phoneUp && !pitchView} />
      </Canvas>

      {/* first person: a small aim dot, and what F does right here */}
      {fp && !overlay && <div className="f3-cross" aria-hidden />}
      {fp && prompt && <div className="f3-aim" data-testid="f3-prompt">{!TOUCH_DEVICE && <kbd>F</kbd>} {prompt}</div>}
      {fp && !overlay && !locked && (TOUCH_DEVICE
        ? <div className="f3-look-hint">拖曳畫面看四周</div>
        : <div className="f3-look-hint">點一下畫面，用滑鼠看四周・<kbd>Q</kbd><kbd>E</kbd> 也能轉身</div>)}

      {/* HUD */}
      <div className="f3-hud" data-testid="f3-hud">
        <div className="f3-hud-top">
          <b>戰情室負責人</b>
          <span className="f3-where" data-testid="f3-where">📍 {SCENE_LABEL[scene]}</span>
          <span className="muted">{ft ? `${fmtMD(ft)} ${fmtHM(ft)}` : '—'}</span>
          {/* the same as the dashboard header: 一日循環 small and muted beside the time, full wording in the tooltip */}
          {state.clock?.day_loop && (
            <span className="f3-loop" data-testid="f3-loop" title={`一日循環・第 ${state.clock.day_loop.loop} 輪：團隊資料集 ${state.clock.day_loop.source_day} 一整天，24:00 回到 00:00 重跑`}>
              <span className="sr">一日循環・</span>第 {state.clock.day_loop.loop} 輪
            </span>
          )}
          {/* paused (0, or the dashboard's ❚❚ 暫停): without this the plant just looks frozen */}
          {paused && <span style={PAUSED_CHIP} role="status" data-testid="f3-paused">❚❚ 已暫停{TOUCH_DEVICE ? '' : '・按 0'}</span>}
          {/* the header's link chip wording (linkStatus.ts); .live = connected to the backend (e2e waits on it) */}
          <span className={`f3-conn t-${link.tone} ${transport?.mode === 'live' && connected ? 'live' : ''}`} title={link.title} data-testid="f3-conn" role="status">{link.text}</span>
        </div>
        <div className={`f3-ev ${evOpen ? 'open' : ''}`} data-testid="f3-event">
          {ev ? <AiStatusLine ev={ev} className="f3-ai" /> : '目前無事件'}
        </div>
        <div className={`f3-pend ${pending.length || reviews.length ? 'has' : ''}`} data-testid="f3-pending">待核准 {pending.length} 件{reviews.length ? `・待覆核 ${reviews.length} 件` : ''}{pending.length || reviews.length ? (TOUCH_DEVICE ? '・點「手機」' : '・按 P 拿手機') : ''}</div>
        {!demoOn && (
          <button className="f3-demo-btn" onClick={() => void demo.begin()} data-testid="demo-start" disabled={!transport}>▶ Demo（6 分鐘）{!TOUCH_DEVICE && <kbd>G</kbd>}</button>
        )}
        {started && !phoneUp && !demoOn && <AnomalyMenu open={anomOpen} onOpen={setAnomOpen} onNote={(text, ms) => setDemoNote({ text, at: Date.now(), ms })} />}
      </div>
      {started && !overlay && TOUCH_DEVICE && <TouchControls action={prompt} mode={mode} />}
      {!demoOn && <div className={`f3-keys${TOUCH_DEVICE ? ' touch' : ''}`}>
        <span><kbd>W A S D</kbd> 走</span><span><kbd>Shift</kbd> 跑</span><span><kbd>滑鼠</kbd>/<kbd>Q E</kbd> 看</span>
        <span><kbd>F</kbd> 操作・進門</span><span><kbd>P</kbd> 手機</span><span><kbd>T</kbd> 觸發異常</span><span><kbd>V</kbd> {mode === 'first' ? '俯瞰' : '第一人稱'}</span><span><kbd>G</kbd> Demo</span>{!pad.connected && !TOUCH_DEVICE && <span className="f3-pad-hint">🎮 按任一鍵連接搖桿</span>}
        {DEMO_MODE && <span className="f3-demo-keys"><kbd>1</kbd> 7 號機溫降 <kbd>5</kbd> 3 件同時 <kbd>6</kbd> 正常運轉 <kbd>2</kbd> 10× <kbd>3</kbd> 60× <kbd>0</kbd> 暫停／播放</span>}
      </div>}
      <DemoOverlay demo={demo} deck={deck} onPhone={() => raisePhone(true)} pad={pad.connected} />
      {!demoOn && scene === 'factory' && alarmIds.map((id) => (
        <button key={id} className={`f3-alarm-arrow ${alarmMap[id].level}`} style={{ display: 'none' }} data-testid={`f3-arrow-${id}`}
          ref={(el) => { if (el) arrowEls.set(id, el); else arrowEls.delete(id) }}
          onClick={() => { setMode('third'); standAt(id) }}>
          <i aria-hidden>➤</i>{alarmMap[id].text}
        </button>
      ))}
      {pad.connected && <div className="f3-pad-chip" data-testid="f3-pad">🎮 已連接{pad.help ? '' : '・按住 View 看按鍵'}</div>}
      {pad.help && <PadHelp demo={demoOn} />}
      {padFocus && !demoOn && <div className="f3-pad-focus" data-testid="f3-pad-focus">{padFocus}</div>}
      {slideView && <SlideView slides={deck.slides} index={Math.min(demo.slide, nSlides - 1)} onIndex={setSlide} onClose={() => setSlideView(false)} />}
      {success.latest && shown !== success.latest.key && (
        <div key={success.latest.key} className="f3-success" role="status" data-testid="f3-success">{successTitle(success.latest)}</div>
      )}
      {demoNote && <div key={demoNote.at} className="f3-demo-note" role="status" data-testid="f3-demo-note">{demoNote.text}</div>}
      {toast && (
        <div className="f3-toast" role="status" data-testid="f3-toast" onClick={() => raisePhone(true)}>
          <b>📱 {toast.title}</b><span>{toast.body}</span><i>{TOUCH_DEVICE ? '點這裡拿起手機' : '按 P 拿起手機'}{toast.review ? '覆核' : '核准'}</i>
        </div>
      )}
      <div className={`f3-fade ${fading ? 'on' : ''}`} aria-hidden data-testid="f3-fade" />

      {!started && (
        <div className="f3-start" onClick={start} data-testid="f3-start">
          <div>
            <h1>迷你工廠・戰情室</h1>
            <p>你是戰情室負責人，從戰情室門口出發。走遍廠房看 28 台機台的即時狀態，坐到戰情室電腦前用真的儀表板。</p>
            {TOUCH_DEVICE ? (
              <ul>
                <li>左下搖桿移動，推到底是跑步</li>
                <li>拖曳畫面轉視角，兩指捏合縮放</li>
                <li>靠近機台或門，右下會出現操作按鈕</li>
                <li>「手機」拿起主管手機（真的手機頁，在哪都能核准）・「第一人稱」切換視角</li>
              </ul>
            ) : <ul>
              <li><kbd>W A S D</kbd> 移動，<kbd>Shift</kbd> 跑步，滑鼠看四周（<kbd>Q</kbd>/<kbd>E</kbd> 也能轉身）</li>
              <li>機台旁按 <kbd>F</kbd> 看即時讀數；戰情室桌前按 <kbd>F</kbd> 坐下用儀表板</li>
              <li>牆上的門通往 <b>休息區</b> 和 <b>廁所</b>：走進去或按 <kbd>F</kbd></li>
              <li><kbd>P</kbd> 拿起主管手機（真的手機頁，在哪都能核准）・<kbd>V</kbd> 切換俯瞰</li>
            </ul>}
            <button className="f3-go">點擊開始</button>
            <button className="f3-go f3-go-demo" data-testid="demo-start-intro" onClick={(e) => { e.stopPropagation(); void demo.begin() }}>▶ Demo（6 分鐘）</button>
          </div>
        </div>
      )}

      {panel?.kind === 'machine' && <MachineDrawer id={panel.id} onClose={closePanel} extra={(d) => <FieldSop machineId={panel.id} tiles={d.tiles} />} />}
      {panel?.kind === 'monitor' && <DeskMonitor onClose={closePanel} />}
      <PhoneOverlay up={phoneUp} onLower={() => raisePhone(false)} buzzing={buzz} />
    </div>
  )
}
