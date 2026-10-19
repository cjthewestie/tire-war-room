// 3D factory walk-through (served at /factory). The player is the war-room lead: walk the hall, press F at a
// machine for its live readings, F at the desk for the demo controls, P to raise the real /phone page.
// All state comes from the same WarRoomProvider as the dashboard (live /ws, or the mock engine).
import { Canvas } from '@react-three/fiber'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { ScenarioName } from '../contracts'
import { DemoStrip } from '../components/DemoStrip'
import { MachineDrawer } from '../components/MachineDrawer'
import { STATE_LABEL } from '../components/common'
import { machineLabel } from '../factory'
import { fmtHM, fmtMD } from '../format'
import { alertText, diffPending, type PushState } from '../phone/alert'
import { primaryEvent } from '../store'
import { useWarRoom } from '../useWarRoom'
import { HandPhone } from './HandPhone'
import { PLACED_BY_ID, type Near } from './layout'
import { Player } from './Player'
import { PhoneOverlay } from './PhoneOverlay'
import { player, teleport } from './pose'
import { World } from './World'

type Panel = { kind: 'machine'; id: string } | { kind: 'desk' } | null

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

function lockPointer(el: HTMLElement | null) {
  try {
    const p = el?.requestPointerLock?.() as unknown
    if (p && typeof (p as Promise<void>).catch === 'function') (p as Promise<void>).catch(() => {})
  } catch { /* not allowed here (headless, iframe): drag-to-look still works */ }
}
function unlockPointer() { try { if (document.pointerLockElement) document.exitPointerLock() } catch { /* ignore */ } }

/** In front of a machine, in the aisle, facing it. */
function standAt(id: string) {
  const p = PLACED_BY_ID[id]
  if (!p) return
  const side = p.line === 'CURE' ? -1 : 1   // presses: stand on the aisle north of the row
  const z = p.z + side * (p.d / 2 + 2.0)
  teleport(p.x, z, side > 0 ? 0 : Math.PI)
  player.pitch = -0.12
}

export function FactoryApp() {
  const { state, transport, connected } = useWarRoom()
  const [started, setStarted] = useState(false)
  const [locked, setLocked] = useState(false)
  const [near, setNear] = useState<Near>(null)
  const [panel, setPanel] = useState<Panel>(null)
  const [phoneUp, setPhoneUp] = useState(false)
  const [toast, setToast] = useState<{ title: string; body: string; at: number } | null>(null)
  const [buzzUntil, setBuzzUntil] = useState(0)
  const [busy, setBusy] = useState(false)
  const [buzz, setBuzz] = useState(false)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const nearRef = useRef<Near>(null)
  useEffect(() => { nearRef.current = near }, [near])
  const push = useRef<PushState>({ seen: new Set(), held: [] })

  const ev = primaryEvent(state.snapshot?.events)
  const evOpen = ev && ev.state !== 'closed' && ev.state !== 'safe_stop'
  const pending = state.pending
  const ft = state.clock?.factory_time ?? state.snapshot?.header.factory_time
  const overlay = panel !== null || phoneUp
  const lanDown = state.snapshot?.header.network === 'lan_down'

  // New approval → phone buzz + ring + HUD toast (same diff the phone page uses).
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
    const f = () => setLocked(document.pointerLockElement === canvasRef.current)
    document.addEventListener('pointerlockchange', f)
    return () => document.removeEventListener('pointerlockchange', f)
  }, [])

  const start = useCallback(() => {
    unlockAudio()
    setStarted(true)
    lockPointer(canvasRef.current)
    canvasRef.current?.focus()
  }, [])

  const raisePhone = useCallback((up: boolean) => {
    setPhoneUp(up)
    if (up) { setPanel(null); unlockPointer() } else { window.focus(); canvasRef.current?.focus() }
  }, [])

  const closePanel = useCallback(() => { setPanel(null); canvasRef.current?.focus() }, [])

  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA')) return
      if (!started && (e.code === 'Enter' || e.code === 'Space')) { start(); return }
      unlockAudio()
      if (e.code === 'KeyP') { raisePhone(!phoneUp); return }
      if (e.code === 'Escape') { if (phoneUp) raisePhone(false); else if (panel?.kind === 'desk') closePanel(); return }
      if (e.code === 'KeyF') {
        if (panel) { closePanel(); return }
        if (phoneUp) return
        const n = nearRef.current
        if (!n) return
        unlockPointer()
        setPanel(n.kind === 'desk' ? { kind: 'desk' } : { kind: 'machine', id: n.id })
      }
    }
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [started, phoneUp, panel, start, raisePhone, closePanel])

  // Test / demo hook (headless e2e moves the player without a mouse).
  useEffect(() => {
    const w = window as unknown as Record<string, unknown>
    w.__factory3d = { teleport, standAt, player, raisePhone }
    return () => { delete w.__factory3d }
  }, [raisePhone])

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    try { await fn() } catch (e) { console.warn(e) } finally { setBusy(false) }
  }
  const scenario = (s: ScenarioName) => run(() => transport!.control({ action: 'scenario', scenario: s }))

  return (
    <div className="f3-root">
      <Canvas
        className="f3-canvas"
        onCreated={({ gl }) => { canvasRef.current = gl.domElement; gl.domElement.tabIndex = 0 }}
        camera={{ fov: 70, near: 0.05, far: 140 }}
        dpr={[1, 1.5]}
        gl={{ antialias: true, powerPreference: 'high-performance' }}
        onClick={() => { if (!overlay) { if (!started) start(); else if (!locked) lockPointer(canvasRef.current) } }}
      >
        <color attach="background" args={['#1a1e24']} />
        <fog attach="fog" args={['#1a1e24', 30, 75]} />
        <World state={state} connected={connected} mode={transport?.mode ?? 'live'} />
        <Player enabled={started && !overlay} onNear={setNear} />
        <HandPhone pending={pending.length} buzzUntil={buzzUntil} hidden={phoneUp} />
      </Canvas>

      {/* HUD */}
      <div className="f3-hud" data-testid="f3-hud">
        <div className="f3-hud-top">
          <b>戰情室負責人</b>
          <span className="muted">{ft ? `${fmtMD(ft)} ${fmtHM(ft)}` : '—'}</span>
          <span className={`f3-conn ${connected ? 'ok' : 'bad'}`}>{connected ? (transport?.mode === 'mock' ? '模擬資料' : '即時連線') : '連線中'}</span>
        </div>
        <div className={`f3-ev ${evOpen ? 'open' : ''}`} data-testid="f3-event">
          {ev ? <>事件：{machineLabel(ev.machine_id)}・{STATE_LABEL[ev.state]}{ev.alert_card ? `・${ev.alert_card.title}` : ''}</> : '目前無事件'}
        </div>
        <div className={`f3-pend ${pending.length ? 'has' : ''}`} data-testid="f3-pending">待核准 {pending.length} 件{pending.length ? '・按 P 拿手機' : ''}</div>
      </div>
      {!overlay && <div className="f3-cross" aria-hidden>+</div>}
      {started && !overlay && near && (
        <div className="f3-prompt" data-testid="f3-prompt">
          <kbd>F</kbd> {near.kind === 'desk' ? '戰情室控制台' : `查看 ${machineLabel(near.id)}`}
        </div>
      )}
      <div className="f3-keys">
        <span><kbd>W A S D</kbd> 走</span><span><kbd>Shift</kbd> 跑</span><span><kbd>滑鼠</kbd>/<kbd>Q E</kbd> 轉</span>
        <span><kbd>F</kbd> 操作</span><span><kbd>P</kbd> 手機</span><span><kbd>Esc</kbd> 放開滑鼠</span>
      </div>
      {toast && (
        <div className="f3-toast" role="status" data-testid="f3-toast" onClick={() => raisePhone(true)}>
          <b>📱 {toast.title}</b><span>{toast.body}</span><i>按 P 拿起手機核准</i>
        </div>
      )}

      {!started && (
        <div className="f3-start" onClick={start} data-testid="f3-start">
          <div>
            <h1>3D 工廠・戰情室</h1>
            <p>你是戰情室負責人。從戰情室桌前出發，走進產線查看 28 台機台的即時狀態。</p>
            <ul>
              <li><kbd>W A S D</kbd> 移動，<kbd>Shift</kbd> 跑步，滑鼠或 <kbd>Q</kbd>/<kbd>E</kbd> 轉向</li>
              <li>走到機台前按 <kbd>F</kbd> 看即時讀數；在戰情室桌前按 <kbd>F</kbd> 開展示控制</li>
              <li><kbd>P</kbd> 拿起主管手機（真的手機頁，可核准、可問戰情室）</li>
            </ul>
            <button className="f3-go">點擊開始</button>
          </div>
        </div>
      )}

      {panel?.kind === 'machine' && <MachineDrawer id={panel.id} onClose={closePanel} />}
      {panel?.kind === 'desk' && (
        <div className="f3-desk-wrap" onClick={closePanel}>
          <section className="f3-desk" role="dialog" aria-label="戰情室控制台" onClick={(e) => e.stopPropagation()}>
            <header><h2>戰情室控制台</h2><button className="x" onClick={closePanel} aria-label="關閉">✕</button></header>
            <p className="muted small">與戰情室畫面上方「展示」列相同的 API（/api/sim/control）。</p>
            <div className="f3-desk-main">
              <button className="primary" disabled={busy || !transport} onClick={() => scenario('press7_temp_drop')}>▶ 啟動 p.11：7 號硫化機溫降</button>
              <button disabled={busy || !transport} onClick={() => run(() => transport!.control({ action: 'reset' }))}>↺ 重來</button>
              <button onClick={() => { standAt('CURE-07'); closePanel() }}>前往 7 號硫化機</button>
              {ev && ev.machine_id !== 'CURE-07' && <button onClick={() => { standAt(ev.machine_id); closePanel() }}>前往事件機台（{machineLabel(ev.machine_id)}）</button>}
            </div>
            <DemoStrip />
            <p className="muted small">戰情室全畫面：<a href={`/${location.search}`} target="_blank" rel="noreferrer">開啟儀表板</a></p>
          </section>
        </div>
      )}
      <PhoneOverlay up={phoneUp} onLower={() => raisePhone(false)} buzzing={buzz} />
    </div>
  )
}
