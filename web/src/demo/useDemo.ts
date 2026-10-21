// The war room side of the guided demo: follow the shared cue (WS "demo"), send continue / pause / exit, and, when the
// demo was started here (no 3D tab driving), run the same cue timeline as the 3D factory (demo/driver.ts) and post each
// cue it reaches. The 3D factory drives with factory3d/useGuidedDemo.ts; whichever screen pressed start is the driver.
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { useWarRoom } from '../useWarRoom'
import { CUES } from './cues'
import { beatFor } from './beats'
import { DemoRun } from './driver'
import { PAD_A, PAD_MENU, padEdges } from './gamepad'
import type { DemoCommand, DemoState } from './state'

/** This tab's driver id: the war room drives only the demo it started, in the tab that started it. */
const ME = `warroom:${Math.random().toString(36).slice(2, 8)}`

export function useDemo() {
  const { state, transport } = useWarRoom()
  const demo: DemoState | null = state.demo
  const running = !!demo?.running
  const [err, setErr] = useState<string | null>(null)
  const send = useCallback(async (c: DemoCommand) => {
    if (!transport?.demo) { setErr('Demo 模式需要連到伺服器（live）'); return null }
    try { setErr(null); return await transport.demo(c) } catch (e) {
      const m = String((e as Error).message ?? e)
      setErr(m.includes('approval') ? '要先核准（手機或核准卡），才能往下' : m.replace(/^\d+：?/, ''))
      return null
    }
  }, [transport])

  // ---------------------------------------------------------------- driver (this tab started the demo): demo/driver.ts
  const run = useRef<DemoRun | null>(null)
  const start = useCallback(async () => {
    if (!transport?.demo) { setErr('Demo 模式需要連到伺服器（live）'); return }
    const r = new DemoRun(transport, ME)
    run.current = r
    const note = await r.start(performance.now())
    if (note) setErr(note)
  }, [transport])
  const cont = useCallback(() => send({ action: 'continue' }), [send])
  // the paused flag the server last sent (a quick P-P must not send pause twice before the push arrives)
  const pausedRef = useRef(false)
  useEffect(() => { pausedRef.current = !!demo?.paused }, [demo?.paused])
  const togglePause = useCallback(async () => {
    const out = await send({ action: pausedRef.current ? 'resume' : 'pause' })
    if (out) pausedRef.current = out.paused
  }, [send])
  const exit = useCallback(async () => { if (run.current) await run.current.exit(); else await send({ action: 'exit' }) }, [send])

  const driving = running && demo?.driver === ME
  const stateRef = useRef(state)
  useEffect(() => { stateRef.current = state }, [state])
  useEffect(() => {
    if (!driving) return
    const id = setInterval(() => {
      const r = run.current
      if (!r) return
      if (r.tick(performance.now(), stateRef.current) === 'stopped') run.current = null
    }, 250)
    return () => clearInterval(id)
  }, [driving])

  // ---------------------------------------------------------------- Xbox pad: A continue, Menu pause (Gamepad API)
  useEffect(() => {
    if (!running || typeof navigator === 'undefined' || !navigator.getGamepads) return
    let prev = new Set<number>()
    const id = setInterval(() => {
      const { held, pressed } = padEdges(prev, [...navigator.getGamepads()])
      prev = held
      if (pressed.includes(PAD_A)) void cont()
      if (pressed.includes(PAD_MENU)) void togglePause()
    }, 80)
    return () => clearInterval(id)
  }, [running, cont, togglePause])

  // ---------------------------------------------------------------- html[data-demo]: other overlays (the global
  // pending-approval bar) go compact while the demo runs
  useEffect(() => {
    const el = document.documentElement
    if (running && demo?.cue_id) el.dataset.demo = demo.cue_id
    else delete el.dataset.demo
    return () => { delete el.dataset.demo }
  }, [running, demo?.cue_id])

  // ---------------------------------------------------------------- keys: Space / N continue, P pause, Esc exit
  useEffect(() => {
    if (!running) return
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
      if (e.key === ' ' || e.key === 'n' || e.key === 'N') { e.preventDefault(); void cont() }
      else if (e.key === 'p' || e.key === 'P') { e.preventDefault(); void togglePause() }
      else if (e.key === 'Escape') { e.preventDefault(); void exit() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [running, cont, togglePause, exit])

  const cue = running ? CUES.find((c) => c.id === demo?.cue_id) ?? null : null
  return { demo, running, cue, beat: running ? beatFor(demo?.cue_id) : null, driving, err, start, cont, togglePause, exit }
}
export type UseDemo = ReturnType<typeof useDemo>

/** The war room runs useDemo once (App's Dashboard) and shares it, so the 「▶ Demo」 button and the overlay drive one
 *  timeline. Null outside the war room (the 3D page's embedded strip has its own Demo button). */
export const DemoCtx = createContext<UseDemo | null>(null)
export const useDemoCtx = () => useContext(DemoCtx)
