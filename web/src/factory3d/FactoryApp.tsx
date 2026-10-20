// 3D factory (served at /factory). You are the war-room lead, in first person, inside a Minimotive-style miniature
// factory building: walk the rows (F at a machine for its live readings), sit at the war-room computer (F at the desk)
// to use the real dashboard, watch it live on the big wall screen, go through the doors to the 休息區 and 廁所
// (separate spaces), and raise the real /phone page (P) wherever you are. V switches to the diorama overview.
// Presenter keys (#25): 1 loads the 7 號硫化機溫降 scenario, 2 / 3 run at 10× / 60×, 0 pauses or plays, so the demo
// never has to leave the 3D view; ?demo=1 also lists them in the key strip.
// Phones and tablets (#26) start in the overview, with a stick and buttons on screen (TouchControls).
// All state comes from the same WarRoomProvider as the dashboard (live /ws, or the mock engine).
import { Canvas } from '@react-three/fiber'
import * as THREE from 'three'
import { useCallback, useEffect, useRef, useState } from 'react'
import { MachineDrawer } from '../components/MachineDrawer'
import { STATE_LABEL } from '../components/common'
import type { SimControl } from '../contracts'
import { machineLabel } from '../factory'
import { fmtHM, fmtMD } from '../format'
import { alertText, diffPending, type PushState } from '../phone/alert'
import { SCENARIOS } from '../mock/engine'
import { primaryEvent } from '../store'
import { useWarRoom } from '../useWarRoom'
import { DeskMonitor } from './DeskMonitor'
import { PLACED_BY_ID, ROOMS, collide, walkable, type Near, type SceneId } from './layout'
import { releaseLook, requestLook } from './look'
import { Player } from './Player'
import { PhoneOverlay } from './PhoneOverlay'
import { TOUCH_DEVICE, cam, player, teleport, view } from './pose'
import { BreakRoom, Restroom } from './Rooms'
import { SPACES, arrival } from './spaces'
import { TiltShift } from './TiltShift'
import { TouchControls } from './TouchControls'
import { DRAWN_LIGHT, Lights, World } from './World'

type Panel = { kind: 'machine'; id: string } | { kind: 'monitor' } | null
const SCENE_LABEL: Record<SceneId, string> = { factory: '工廠', break: '休息區', toilet: '廁所' }
const PRESS7 = (SCENARIOS.find((s) => s.name === 'press7_temp_drop')?.label ?? '7 號硫化機溫降').replace(/（.*?）/, '')
/** Presenter keys → the same /api/sim/control calls as the dashboard's demo strip. Digit0 toggles, so it is built per press. */
const DEMO_KEYS: Record<string, (paused: boolean) => { c: SimControl; label: string }> = {
  Digit1: () => ({ c: { action: 'scenario', scenario: 'press7_temp_drop' }, label: `載入情境：${PRESS7}` }),
  Digit2: () => ({ c: { action: 'speed', speed: 10 }, label: '10 倍速' }),
  Digit3: () => ({ c: { action: 'speed', speed: 60 }, label: '60 倍速' }),
  Digit0: (paused) => ({ c: { action: paused ? 'play' : 'pause' }, label: paused ? '繼續播放' : '暫停' }),
}
const SHOW_DEMO_KEYS = new URLSearchParams(location.search).get('demo') === '1'

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
  const [started, setStarted] = useState(false)
  const [near, setNear] = useState<Near>(null)
  const [panel, setPanel] = useState<Panel>(null)
  const [phoneUp, setPhoneUp] = useState(false)
  const [mode, setMode] = useState<'first' | 'third'>(TOUCH_DEVICE ? 'third' : 'first')
  const [scene, setScene] = useState<SceneId>('factory')
  const [fading, setFading] = useState(false)
  const [locked, setLocked] = useState(false)
  const [toast, setToast] = useState<{ title: string; body: string; at: number } | null>(null)
  const [buzzUntil, setBuzzUntil] = useState(0)
  const [buzz, setBuzz] = useState(false)
  const [demoNote, setDemoNote] = useState<{ text: string; at: number } | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const cameraRef = useRef<THREE.Camera | null>(null)
  const nearRef = useRef<Near>(null)
  useEffect(() => { nearRef.current = near }, [near])
  const fadingRef = useRef(false)
  const push = useRef<PushState>({ seen: new Set(), held: [] })

  const ev = primaryEvent(state.snapshot?.events)
  const evOpen = ev && ev.state !== 'closed' && ev.state !== 'safe_stop'
  const pending = state.pending
  const ft = state.clock?.factory_time ?? state.snapshot?.header.factory_time
  const overlay = panel !== null || phoneUp
  const lanDown = state.snapshot?.header.network === 'lan_down'
  const fp = started && mode === 'first'
  const paused = !!state.clock?.paused

  // New approval → phone buzz + ring + HUD toast (same diff the phone page uses), in every space.
  useEffect(() => {
    const out = diffPending(push.current, pending, connected && !lanDown)
    if (!out.fire.length) return
    const { title, body } = alertText(out.fire, out.flushed)
    setToast({ title, body, at: Date.now() })
    setBuzzUntil(performance.now() + 2200)
    setBuzz(true)
    try { navigator.vibrate?.([200, 100, 200]) } catch { /* unsupported */ }
    ring()
  }, [pending, connected, lanDown])
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
    if (!demoNote) return
    const t = setTimeout(() => setDemoNote(null), 2500)
    return () => clearTimeout(t)
  }, [demoNote])

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
      const demo = DEMO_KEYS[e.code]
      if (demo) {
        if (!transport || e.repeat) return
        const { c, label } = demo(paused)
        transport.control(c).then(() => setDemoNote({ text: label, at: Date.now() }), (err) => { console.warn(err); setDemoNote({ text: `${label}：沒有成功`, at: Date.now() }) })
        return
      }
      if (e.code === 'Digit4') {          // issue #36: 隨機事件 on / off (the same switch as the demo strip)
        if (!transport?.setSimEvents || e.repeat) return
        const on = !state.clock?.random_events?.enabled
        transport.setSimEvents({ enabled: on }).then(
          (v) => setDemoNote({ text: v.enabled ? `隨機事件：開（每 ${v.every_min} 分）` : '隨機事件：關', at: Date.now() }),
          (err) => { console.warn(err); setDemoNote({ text: '隨機事件：沒有成功', at: Date.now() }) })
        return
      }
      if (e.code === 'KeyP') { raisePhone(!phoneUp); return }
      if (e.code === 'Escape') { if (phoneUp) raisePhone(false); else if (panel) closePanel(); return }
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
  }, [started, phoneUp, panel, overlay, start, raisePhone, closePanel, goTo, transport, paused, state.clock?.random_events?.enabled])

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
        <World state={state} active={scene === 'factory'} fp={fp} focusId={started && !overlay && near?.kind === 'machine' ? near.id : null} />
        {scene === 'break' && <BreakRoom />}
        {scene === 'toilet' && <Restroom />}
        <Player
          enabled={started && !overlay && !fading} started={started} onNear={setNear} onDoor={goTo} phoneUp={phoneUp}
          pending={pending.length} buzzUntil={buzzUntil} mode={mode} scene={scene}
          prompt={prompt}
        />
        <TiltShift enabled={(!started || mode === 'third') && !phoneUp} />
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
          <span className={`f3-conn ${connected ? 'ok' : 'bad'}`}>{connected ? (transport?.mode === 'mock' ? '模擬資料' : '即時連線') : '連線中'}</span>
        </div>
        <div className={`f3-ev ${evOpen ? 'open' : ''}`} data-testid="f3-event">
          {ev ? <>事件：{machineLabel(ev.machine_id)}・{STATE_LABEL[ev.state]}{ev.alert_card ? `・${ev.alert_card.title}` : ''}</> : '目前無事件'}
        </div>
        <div className={`f3-pend ${pending.length ? 'has' : ''}`} data-testid="f3-pending">待核准 {pending.length} 件{pending.length ? (TOUCH_DEVICE ? '・點「手機」' : '・按 P 拿手機') : ''}</div>
      </div>
      {started && !overlay && TOUCH_DEVICE && <TouchControls action={prompt} mode={mode} />}
      <div className={`f3-keys${TOUCH_DEVICE ? ' touch' : ''}`}>
        <span><kbd>W A S D</kbd> 走</span><span><kbd>Shift</kbd> 跑</span><span><kbd>滑鼠</kbd>/<kbd>Q E</kbd> 看</span>
        <span><kbd>F</kbd> 操作・進門</span><span><kbd>P</kbd> 手機</span><span><kbd>V</kbd> {mode === 'first' ? '俯瞰' : '第一人稱'}</span>
        {SHOW_DEMO_KEYS && <span className="f3-demo-keys"><kbd>1</kbd> 7 號機溫降 <kbd>2</kbd> 10× <kbd>3</kbd> 60× <kbd>4</kbd> 隨機事件 <kbd>0</kbd> 暫停／播放</span>}
      </div>
      {demoNote && <div key={demoNote.at} className="f3-demo-note" role="status" data-testid="f3-demo-note">{demoNote.text}</div>}
      {toast && (
        <div className="f3-toast" role="status" data-testid="f3-toast" onClick={() => raisePhone(true)}>
          <b>📱 {toast.title}</b><span>{toast.body}</span><i>{TOUCH_DEVICE ? '點這裡拿起手機核准' : '按 P 拿起手機核准'}</i>
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
          </div>
        </div>
      )}

      {panel?.kind === 'machine' && <MachineDrawer id={panel.id} onClose={closePanel} />}
      {panel?.kind === 'monitor' && <DeskMonitor onClose={closePanel} />}
      <PhoneOverlay up={phoneUp} onLower={() => raisePhone(false)} buzzing={buzz} />
    </div>
  )
}
