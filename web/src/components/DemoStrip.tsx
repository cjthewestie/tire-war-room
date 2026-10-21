import { useState } from 'react'
import type { HeaderStatus, ScenarioName } from '../contracts'
import { SCENARIOS } from '../mock/engine'
import { useWarRoom } from '../useWarRoom'
import { overlayNote, runningScenarios, scenarioClick } from './scenarioOverlay'
import { SimEventControls } from './SimEvents'

const SPEEDS = [1, 5, 10, 60]
const Q = new URLSearchParams(location.search)
/** ?demo=1: the 3D presenter mode (key strip listed, key 4 隨機事件 off). The demo strip no longer depends on it. */
export const DEMO_MODE = Q.get('demo') === '1'
/** ?scenarios=all (developer; the 3D desk, wall screen and phone iframes inherit it): every scenario plus the
 *  day-loop jumps and the 隨機事件／觸發事件 controls. Without it the strip is the presenter's: three scenarios only. */
export const ALL_SCENARIOS = Q.get('scenarios') === 'all'
const DEFAULT_SCENARIOS: ScenarioName[] = ['normal', 'press7_temp_drop', 'concurrent_3']
const JUMPS: Partial<Record<ScenarioName, { label: string; iso: string }[]>> = {
  press7_temp_drop: [
    { label: '02:14 偵測', iso: '2026-10-03T02:13:55+08:00' },
    { label: '02:16 核准', iso: '2026-10-03T02:16:30+08:00' },
    { label: '02:31 驗證失敗', iso: '2026-10-03T02:30:55+08:00' },
  ],
}

// issue #36 一日循環: jump to a time of day (the plant runs forward through it, 10 s steps; past midnight it wraps)
const DAY_JUMPS = [
  { at: '07:55', label: '08:00 開工' }, { at: '09:10', label: '10:00 出貨' },
  { at: '15:10', label: '16:00 出貨' }, { at: '23:55', label: '24:00 換日' },
]

export function DemoStrip() {
  const { state, transport } = useWarRoom()
  const [scenario, setScenario] = useState<ScenarioName | null>(null)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState<string | null>(null)   // 情境切換中: the server builds the new plant
  const [overlaid, setOverlaid] = useState<string | null>(null)  // 情境疊加: 「已疊加：…（新增 N 件）」
  if (!transport) return null
  const clock = state.clock
  const net = state.snapshot?.header.network ?? 'online'
  const mock = transport.mode === 'mock'
  const run = async (fn: () => Promise<unknown>) => { setBusy(true); try { await fn() } catch (e) { console.warn(e) } finally { setBusy(false) } }
  const current = scenario ?? (state.clock?.scenario as ScenarioName | undefined) ?? (mock ? guessScenario(state.snapshot?.events.map((e) => e.machine_id) ?? []) : null)
  const setNet = (n: HeaderStatus['network']) => run(() => transport.setNetwork(n))
  // a scenario picked while paused used to load and stay paused (the plant looked frozen): loading it also plays
  const loadAndPlay = async (name: ScenarioName, speed: number) => {
    await transport.control({ action: 'scenario', scenario: name, speed, preroll: true })
    await transport.control({ action: 'play' })
  }
  // 情境疊加: a different overlayable scenario while an event or approval is open adds on top; otherwise load as before
  const pick = (s: (typeof SCENARIOS)[number]) => {
    if (scenarioClick(s.name, state, current) === 'overlay') {
      setLoading(`疊加 ${s.label}`)
      run(async () => {
        try { setOverlaid(overlayNote(s.label, await transport.overlay(s.name))) } catch (e) { setOverlaid(`疊加沒有成功：${s.label}`); throw e }
      }).finally(() => setLoading(null))
      return
    }
    setScenario(s.name); setOverlaid(null); setLoading(s.label)
    run(() => loadAndPlay(s.name, s.speed)).finally(() => setLoading(null))
  }
  const overlays = runningScenarios(state).overlays
  return (
    <div className="demo" aria-label="展示控制">
      <span className="demo-k">展示</span>
      <div className="seg" role="group" aria-label="情境（注入故障）">
        {SCENARIOS.filter((s) => ALL_SCENARIOS || DEFAULT_SCENARIOS.includes(s.name)).map((s) => (
          <button key={s.name} disabled={busy} className={current === s.name ? 'on' : ''} onClick={() => pick(s)}>{s.label}</button>
        ))}
      </div>
      {loading && <span className="demo-loading" role="status" data-testid="demo-loading">{loading.startsWith('疊加 ') ? `${loading}…` : `載入情境…（${loading}）`}</span>}
      {overlaid && (overlays.length > 0 || overlaid.startsWith('疊加沒有成功')) && !loading && (
        <span className="demo-overlay" role="status" data-testid="demo-overlay" title={overlaid}>{overlaid}</span>
      )}
      <div className="seg" role="group" aria-label="播放">
        <button disabled={busy} onClick={() => run(() => transport.control({ action: clock?.paused ? 'play' : 'pause' }))}>{clock?.paused ? '▶ 播放' : '❚❚ 暫停'}</button>
        {SPEEDS.map((s) => (
          <button key={s} disabled={busy} className={clock?.speed === s ? 'on' : ''} onClick={() => run(() => transport.control({ action: 'speed', speed: s }))}>{s}×</button>
        ))}
        <button disabled={busy} onClick={() => { setOverlaid(null); run(() => transport.control({ action: 'reset' })) }}>↺ 重來</button>
      </div>
      {!mock && ALL_SCENARIOS && state.clock?.day_loop && (
        <div className="seg" role="group" aria-label="跳到時間（一日循環）" data-testid="day-jumps">
          {DAY_JUMPS.map((j) => (
            <button key={j.at} disabled={busy} title={`快轉到 ${j.at}（照常跑過中間的時間）`}
              onClick={() => { setLoading(`跳到 ${j.at}`); run(() => transport.control({ action: 'jump', at: j.at })).finally(() => setLoading(null)) }}>{j.label}</button>
          ))}
        </div>
      )}
      {mock && current && JUMPS[current] && (
        <div className="seg" role="group" aria-label="跳到">
          {JUMPS[current]!.map((j) => <button key={j.iso} onClick={() => run(() => transport.jump!(Date.parse(j.iso)))}>{j.label}</button>)}
        </div>
      )}
      {ALL_SCENARIOS && <SimEventControls busy={busy} run={run} />}
      <div className="seg net" role="group" aria-label="拔網路測試" title="模擬離線分層（p.9）：弱網→Agent 不受影響，只延後同步與推播；WAN 斷→手機走廠內 LAN；LAN 斷→現場觸控螢幕＋警示燈">
        {(['online', 'weak', 'wan_down', 'lan_down'] as const).map((n) => (
          <button key={n} disabled={busy} className={net === n ? `on ${n}` : ''} onClick={() => setNet(n)}
            title={n === 'weak' ? '弱網・延後推播：手機與遠端畫面延後數秒批次更新，事件處理與安全核心不受影響' : undefined}>
            {n === 'online' ? '● 在線' : n === 'weak' ? '弱網' : n === 'wan_down' ? 'WAN 斷' : 'LAN 斷'}
          </button>
        ))}
      </div>
      {state.lastAlert && (
        <span className="demo-alert" title={state.lastAlert.summary}>⚡ 最新告警卡：{state.lastAlert.title}</span>
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
