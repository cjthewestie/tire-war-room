// 觸發異常 (#71): an on-screen button for the demo, so the presenter (or a phone / tablet) can start an anomaly without
// remembering the 1–4 keys. It only calls what the dashboard's demo strip already calls: /api/sim/control for the
// scenarios and the speed, /api/sim/events/fire for a single event. T opens it from the keyboard (and frees the mouse).
// 情境疊加: a scenario picked while another one's event or approval is open is added on top (scenarioOverlay.ts).
import { useState } from 'react'
import type { ScenarioName, SimControl } from '../contracts'
import { DEMO_MODE } from '../components/DemoStrip'
import { OVERLAY_NOTE_MS, overlayNote, scenarioClick } from '../components/scenarioOverlay'
import { useSimEvents } from '../components/useSimEvents'
import { SCENARIOS } from '../mock/engine'
import { useWarRoom } from '../useWarRoom'

const ANOMALIES: ScenarioName[] = ['press7_temp_drop', 'open_mill_overheat', 'concurrent_3']
const label = (n: ScenarioName) => (SCENARIOS.find((s) => s.name === n)?.label ?? n).replace(/（.*?）/, '')

export function AnomalyMenu({ open, onOpen, onNote }: {
  open: boolean
  onOpen: (open: boolean) => void
  onNote: (text: string, ms?: number) => void
}) {
  const { state, transport } = useWarRoom()
  const ev = useSimEvents()
  const [busy, setBusy] = useState(false)
  if (!transport) return null
  const paused = !!state.clock?.paused
  /** fn may return the note to show when it is done (情境疊加: what was added).
   *  #85: only a scenario load (or an overlay) shows a wait note first (`wait`, the whole note); speed / pause / reset /
   *  single events are instant */
  const run = async (text: string, fn: () => Promise<unknown>, wait: string | null = null) => {
    setBusy(true)
    onOpen(false)
    if (wait !== null) onNote(wait)
    try {
      const done = await fn()
      if (typeof done === 'string') onNote(done, OVERLAY_NOTE_MS)
      else onNote(text)
    } catch (e) { console.warn(e); onNote(`${text}：沒有成功`) } finally { setBusy(false) }
  }
  const control = (c: SimControl, text: string, wait: string | null = null) => run(text, () => transport.control(c), wait)
  const scenario = (n: ScenarioName) => {
    // 情境疊加: another scenario's event or approval still open → add this one on top (the demo strip's rule)
    if (scenarioClick(n, state) === 'overlay') return run(`疊加異常：${label(n)}`, async () => overlayNote(label(n), await transport.overlay(n)), `疊加異常：${label(n)}…`)
    const s = SCENARIOS.find((x) => x.name === n)
    // demo: say what is happening, not that the first load is slow
    return control({ action: 'scenario', scenario: n, speed: s?.speed ?? 10, preroll: true }, DEMO_MODE ? `已觸發：${label(n)}` : `載入異常：${label(n)}`,
      DEMO_MODE ? `正在載入：${label(n)}` : `載入異常：${label(n)}…（載入中，第一次較久）`)
  }
  return (
    <div className="f3-anom" data-testid="f3-anomaly">
      <button className={`f3-anom-btn${open ? ' on' : ''}`} disabled={busy} aria-expanded={open} data-testid="f3-anomaly-btn"
        onClick={() => onOpen(!open)}>
        ⚡ 觸發異常 <kbd>T</kbd>
      </button>
      {open && (
        <div className="f3-anom-menu" role="menu" data-testid="f3-anomaly-menu">
          <p>{DEMO_MODE ? '選一個異常開始' : '選一個異常開始 Demo'}</p>
          {ANOMALIES.map((n) => (
            <button key={n} role="menuitem" disabled={busy} data-scenario={n} onClick={() => void scenario(n)}>{label(n)}</button>
          ))}
          {ev.live && ev.catalog.length > 0 && <>
            <p>{DEMO_MODE ? '再加一件異常' : '單一事件（照目前情境加一件）'}</p>
            {ev.catalog.map((c) => (
              <button key={c.kind} role="menuitem" disabled={busy} data-kind={c.kind}
                onClick={() => void run(`已觸發：${c.label}`, () => ev.fire(c.kind))}>
                {c.label}<small>{c.source === 'fault' ? '機台' : c.source === 'world' ? '原料' : '工廠'}</small>
              </button>
            ))}
          </>}
          <div className="f3-anom-row">
            <button disabled={busy} onClick={() => void control({ action: 'speed', speed: 10 }, '10 倍速')}>10×</button>
            <button disabled={busy} onClick={() => void control({ action: paused ? 'play' : 'pause' }, paused ? '繼續播放' : '暫停')}>{paused ? '▶ 播放' : '❚❚ 暫停'}</button>
            <button disabled={busy} onClick={() => void control({ action: 'reset' }, '重來')}>↺ 重來</button>
          </div>
        </div>
      )}
    </div>
  )
}
