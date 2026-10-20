import { useState } from 'react'
import type { HeaderStatus, ScenarioName } from '../contracts'
import { SCENARIOS } from '../mock/engine'
import { useWarRoom } from '../useWarRoom'
import { SimEventControls } from './SimEvents'

const SPEEDS = [1, 5, 10, 60]
const JUMPS: Partial<Record<ScenarioName, { label: string; iso: string }[]>> = {
  press7_temp_drop: [
    { label: '02:14 偵測', iso: '2026-10-03T02:13:55+08:00' },
    { label: '02:16 核准', iso: '2026-10-03T02:16:30+08:00' },
    { label: '02:31 驗證失敗', iso: '2026-10-03T02:30:55+08:00' },
  ],
}

export function DemoStrip() {
  const { state, transport } = useWarRoom()
  const [scenario, setScenario] = useState<ScenarioName | null>(null)
  const [busy, setBusy] = useState(false)
  if (!transport) return null
  const clock = state.clock
  const net = state.snapshot?.header.network ?? 'online'
  const mock = transport.mode === 'mock'
  const run = async (fn: () => Promise<unknown>) => { setBusy(true); try { await fn() } catch (e) { console.warn(e) } finally { setBusy(false) } }
  const current = scenario ?? (mock ? guessScenario(state.snapshot?.events.map((e) => e.machine_id) ?? []) : null)
  const setNet = (n: HeaderStatus['network']) => run(() => transport.setNetwork(n))
  return (
    <div className="demo" aria-label="展示控制">
      <span className="demo-k">展示</span>
      <div className="seg" role="group" aria-label="情境（注入故障）">
        {SCENARIOS.map((s) => (
          <button key={s.name} disabled={busy} className={current === s.name ? 'on' : ''}
            onClick={() => { setScenario(s.name); run(() => transport.control({ action: 'scenario', scenario: s.name })) }}>{s.label}</button>
        ))}
      </div>
      <div className="seg" role="group" aria-label="播放">
        <button disabled={busy} onClick={() => run(() => transport.control({ action: clock?.paused ? 'play' : 'pause' }))}>{clock?.paused ? '▶ 播放' : '❚❚ 暫停'}</button>
        {SPEEDS.map((s) => (
          <button key={s} disabled={busy} className={clock?.speed === s ? 'on' : ''} onClick={() => run(() => transport.control({ action: 'speed', speed: s }))}>{s}×</button>
        ))}
        <button disabled={busy} onClick={() => run(() => transport.control({ action: 'reset' }))}>↺ 重來</button>
      </div>
      {mock && current && JUMPS[current] && (
        <div className="seg" role="group" aria-label="跳到">
          {JUMPS[current]!.map((j) => <button key={j.iso} onClick={() => run(() => transport.jump!(Date.parse(j.iso)))}>{j.label}</button>)}
        </div>
      )}
      <SimEventControls busy={busy} run={run} />
      <div className="seg net" role="group" aria-label="拔網路測試" title="模擬離線分層（p.9）：弱網→Agent 不受影響，只延後同步與推播；WAN 斷→手機走廠內 LAN；LAN 斷→現場觸控螢幕＋警示燈">
        {(['online', 'weak', 'wan_down', 'lan_down'] as const).map((n) => (
          <button key={n} disabled={busy} className={net === n ? `on ${n}` : ''} onClick={() => setNet(n)}
            title={n === 'weak' ? '弱網・延後推播：手機與遠端畫面延後數秒批次更新，事件處理與安全核心不受影響' : undefined}>
            {n === 'online' ? '● 在線' : n === 'weak' ? '弱網' : n === 'wan_down' ? 'WAN 斷' : 'LAN 斷'}
          </button>
        ))}
      </div>
      {state.lastAlert && (
        <span className="demo-alert" title={state.lastAlert.summary}>⚡ 首張告警卡：{state.lastAlert.title}</span>
      )}
    </div>
  )
}

function guessScenario(machines: string[]): ScenarioName {
  if (machines.length >= 3) return 'concurrent_3'
  if (machines.includes('CURE-07')) return 'press7_temp_drop'
  if (machines.includes('A-BIGOPEN')) return 'open_mill_overheat'
  return 'normal'
}
