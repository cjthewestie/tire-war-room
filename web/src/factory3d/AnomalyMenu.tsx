// 觸發異常 (#71): an on-screen button for the demo, so the presenter (or a phone / tablet) can start an anomaly without
// remembering the 1–4 keys. It only calls what the dashboard's demo strip already calls: /api/sim/control for the
// scenarios and the speed, /api/sim/events/fire for a single event. T opens it from the keyboard (and frees the mouse).
import { useState } from 'react'
import type { ScenarioName, SimControl } from '../contracts'
import { useSimEvents } from '../components/useSimEvents'
import { SCENARIOS } from '../mock/engine'
import { useWarRoom } from '../useWarRoom'

const ANOMALIES: ScenarioName[] = ['press7_temp_drop', 'open_mill_overheat', 'concurrent_3']
const label = (n: ScenarioName) => (SCENARIOS.find((s) => s.name === n)?.label ?? n).replace(/（.*?）/, '')

export function AnomalyMenu({ open, onOpen, onNote }: {
  open: boolean
  onOpen: (open: boolean) => void
  onNote: (text: string) => void
}) {
  const { state, transport } = useWarRoom()
  const ev = useSimEvents()
  const [busy, setBusy] = useState(false)
  if (!transport) return null
  const paused = !!state.clock?.paused
  // #85: only loading a scenario takes a moment (the first time; #79 reuses it after that); the rest is instant
  const run = async (text: string, fn: () => Promise<unknown>, loading = false) => {
    setBusy(true)
    onOpen(false)
    if (loading) onNote(`${text}…（載入中，第一次較久）`)
    try { await fn(); onNote(text) } catch (e) { console.warn(e); onNote(`${text}：沒有成功`) } finally { setBusy(false) }
  }
  const control = (c: SimControl, text: string, loading = false) => run(text, () => transport.control(c), loading)
  const scenario = (n: ScenarioName) => {
    const s = SCENARIOS.find((x) => x.name === n)
    return control({ action: 'scenario', scenario: n, speed: s?.speed ?? 10, preroll: true }, `載入異常：${label(n)}`, true)
  }
  return (
    <div className="f3-anom" data-testid="f3-anomaly">
      <button className={`f3-anom-btn${open ? ' on' : ''}`} disabled={busy} aria-expanded={open} data-testid="f3-anomaly-btn"
        onClick={() => onOpen(!open)}>
        ⚡ 觸發異常 <kbd>T</kbd>
      </button>
      {open && (
        <div className="f3-anom-menu" role="menu" data-testid="f3-anomaly-menu">
          <p>選一個異常開始 Demo</p>
          {ANOMALIES.map((n) => (
            <button key={n} role="menuitem" disabled={busy} data-scenario={n} onClick={() => void scenario(n)}>{label(n)}</button>
          ))}
          {ev.live && ev.catalog.length > 0 && <>
            <p>單一事件（照目前情境加一件）</p>
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
