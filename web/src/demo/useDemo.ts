// The war room side of the guided demo: follow the shared cue (WS "demo"), send continue / pause / exit, and, when the
// demo was started here (no 3D tab driving), run the same cue timeline as the 3D factory (cues.ts step()) and post each
// cue it reaches. The 3D factory drives with factory3d/useGuidedDemo.ts; whichever screen pressed start is the driver.
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { ScenarioName } from '../contracts'
import { useWarRoom } from '../useWarRoom'
import {
  CUES, DEMO_TOTAL, FALLBACK_LOADS, MAIN_LOADS, PITCH_AT, clockPause, clockRead, clockResume, clockSet, clockStart,
  skip, step, type DemoClock, type DemoLoad, type Timeline,
} from './cues'
import { beatFor, waitingFor } from './beats'
import { freshSeen, signals } from './signals'
import type { DemoCommand, DemoState } from './state'

/** This tab's driver id: the war room drives only the demo it started, in the tab that started it. */
const ME = `warroom:${Math.random().toString(36).slice(2, 8)}`

const waitOf = (i: number) => (CUES[i] ? waitingFor(CUES[i].id, CUES[i + 1]?.on) : null)

/** Move the timeline to cue `i` (a continue pressed on another screen). */
function jumpTo(tl: Timeline, i: number, t: number): Timeline {
  let out = tl
  while (out.index < i && CUES[out.index + 1]) out = skip(out, t)
  return out
}

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

  const start = useCallback(async () => {
    await send({ action: 'start', cues: CUES.map((c) => c.id), cue_id: CUES[0].id, driver: ME, story_target_s: PITCH_AT, waiting_for: waitOf(0) })
  }, [send])
  const cont = useCallback(() => send({ action: 'continue' }), [send])
  const togglePause = useCallback(() => send({ action: demo?.paused ? 'resume' : 'pause' }), [send, demo?.paused])
  const exit = useCallback(() => send({ action: 'exit' }), [send])

  // ---------------------------------------------------------------- driver (this tab started the demo)
  const driving = running && demo?.driver === ME
  const tl = useRef<Timeline | null>(null)
  const clock = useRef<DemoClock>({ base: 0, since: null })
  const seen = useRef(freshSeen())
  const loaded = useRef(new Set<number>())
  const loads = useRef<DemoLoad[]>(MAIN_LOADS)
  const armedAt = useRef(Infinity)
  const stateRef = useRef(state)
  useEffect(() => { stateRef.current = state }, [state])
  const demoRef = useRef(demo)
  useEffect(() => { demoRef.current = demo }, [demo])

  const load = useCallback(async (l: DemoLoad) => {
    if (!transport) return
    armedAt.current = Infinity
    seen.current = freshSeen()
    await transport.control({ action: 'scenario', scenario: l.scenario as ScenarioName, speed: l.speed, preroll: true })
    await transport.control({ action: 'play' })
    armedAt.current = performance.now() + 1500     // the new plant's snapshot replaces the old one's events
  }, [transport])

  useEffect(() => {
    if (!driving || !transport) { tl.current = null; return }
    if (tl.current) return
    tl.current = { index: 0, fired: [{ id: CUES[0].id, t: 0, reason: 'time' }] }
    clock.current = clockStart(performance.now())
    loaded.current = new Set([0])
    void (async () => {
      let known = false
      try { known = (await fetch('/api/sim/scenarios').then((r) => r.json()) as { name: string }[]).some((s) => s.name === MAIN_LOADS[0].scenario) } catch { /* offline */ }
      loads.current = known ? MAIN_LOADS : FALLBACK_LOADS
      try { await load(loads.current[0]) } catch (e) { console.warn('[demo] scenario load', e); setErr('情境載入沒有成功') }
    })()
  }, [driving, transport, load])

  useEffect(() => {
    if (!driving) return
    const id = setInterval(() => {
      const cur = tl.current, d = demoRef.current
      if (!cur || !d) return
      const now = performance.now()
      // the shared pause freezes the timeline too
      if (d.paused && clock.current.since != null) clock.current = clockPause(clock.current, now)
      if (!d.paused && clock.current.since == null) clock.current = clockResume(clock.current, now)
      let t = Math.min(clockRead(clock.current, now), DEMO_TOTAL + 0.5)
      // continue pressed elsewhere: follow the server's cue (into the pitch, the clock jumps there as in the 3D)
      if (d.cue_index > cur.index) {
        tl.current = jumpTo(cur, d.cue_index, t)
        const at = CUES[d.cue_index]?.at ?? t
        if (at >= PITCH_AT && t < at) { clock.current = clockSet(clock.current, at, now); t = at }
        return
      }
      loads.current.forEach((l, i) => {
        if (i > 0 && t >= l.at && !loaded.current.has(i)) { loaded.current.add(i); void load(l).catch((e) => console.warn(e)) }
      })
      const sig = now >= armedAt.current ? signals(stateRef.current, seen.current) : {}
      let n = step(cur, t, sig)
      // the approval beat never moves on by the clock: only the real approval (worker on the way) ends it
      if (n !== cur && waitOf(cur.index) === 'approval' && n.fired[n.fired.length - 1].reason === 'time' && !sig.worker) n = cur
      if (n === cur) return
      tl.current = n
      const c = CUES[n.index]
      void send({ action: 'cue', cue_id: c.id, cue_index: n.index, waiting_for: waitOf(n.index) })
    }, 250)
    return () => clearInterval(id)
  }, [driving, load, send])

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
