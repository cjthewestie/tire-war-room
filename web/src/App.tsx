// War-room dashboard (proposal page 2) + 排程與人力 tab (pages 4, 6, 11).
import { lazy, Suspense, useEffect, useState } from 'react'
import type { LocalItem } from './agent/transcript'
import { DemoStrip } from './components/DemoStrip'
import { Header, type Tab } from './components/Header'
import { MachineDrawer } from './components/MachineDrawer'
import { MonitorTab } from './components/MonitorTab'
import { ScheduleTab } from './components/ScheduleTab'
import { useWarRoom, WarRoomProvider } from './useWarRoom'

const AgentTab = lazy(() => import('./agent/AgentTab').then((m) => ({ default: m.AgentTab })))

function readTab(): Tab { return location.hash === '#schedule' ? 'schedule' : location.hash === '#agent' ? 'agent' : 'monitor' }

function Dashboard() {
  const [tab, setTab] = useState<Tab>(readTab)
  const [machine, setMachine] = useState<string | null>(() => new URLSearchParams(location.search).get('machine'))
  const { state } = useWarRoom()
  useEffect(() => {
    const f = () => setTab(readTab())
    window.addEventListener('hashchange', f)
    return () => window.removeEventListener('hashchange', f)
  }, [])
  const go = (t: Tab) => { history.replaceState(null, '', t === 'monitor' ? location.pathname + location.search : `${location.search}#${t}`); setTab(t) }
  const [asks, setAsks] = useState<LocalItem[]>([])
  const [agentEvent, setAgentEvent] = useState<string | null>(null)
  const openAgent = (eventId?: string) => { if (eventId) setAgentEvent(eventId); go('agent') }
  const [demoOpen, setDemoOpen] = useState(false)
  const lanDown = state.snapshot?.header.network === 'lan_down'
  const pendingN = state.pending.length
  return (
    <div className={`app ${lanDown ? 'lan-down' : ''} ${demoOpen ? 'demo-open' : ''}`}>
      <Header tab={tab} onTab={go} />
      <div className="demo-sheet" role="region" aria-label="展示控制">
        <div className="demo-sheet-h"><b>展示控制</b><button className="x" onClick={() => setDemoOpen(false)} aria-label="關閉展示控制">✕</button></div>
        <DemoStrip />
      </div>
      {demoOpen && <div className="demo-scrim" onClick={() => setDemoOpen(false)} aria-hidden />}
      {lanDown && <div className="banner">LAN 中斷：手機連不上，核准改在現場觸控螢幕操作，警示燈已亮。AI 與安全核心照常在邊緣主機運作。</div>}
      <main>{tab === 'monitor' ? <MonitorTab onMachine={setMachine} onAgent={openAgent} /> : tab === 'schedule' ? <ScheduleTab />
        : <Suspense fallback={<div className="loading">載入 Agent 對話台…</div>}><AgentTab asks={asks} setAsks={setAsks} picked={agentEvent} setPicked={setAgentEvent} /></Suspense>}</main>
      {machine && <MachineDrawer id={machine} onClose={() => setMachine(null)} />}
      <footer className="foot-note">示意畫面；剩餘時間依硫化機產能與班別估算・物理與排程數值由公式與求解器計算，不由 AI 推測</footer>
      <nav className="bottomnav" aria-label="分頁">
        <button className={tab === 'monitor' ? 'on' : ''} aria-current={tab === 'monitor' ? 'page' : undefined} onClick={() => go('monitor')}>
          <svg viewBox="0 0 24 24" aria-hidden><path d="M3 13h4l3-8 4 14 3-6h4" /></svg>產線監控{pendingN > 0 && <i className="nav-badge">{pendingN}</i>}
        </button>
        <button className={tab === 'schedule' ? 'on' : ''} aria-current={tab === 'schedule' ? 'page' : undefined} onClick={() => go('schedule')}>
          <svg viewBox="0 0 24 24" aria-hidden><path d="M4 6h10M4 12h16M4 18h7" /></svg>排程與人力
        </button>
        <button className={tab === 'agent' ? 'on' : ''} aria-current={tab === 'agent' ? 'page' : undefined} onClick={() => go('agent')}>
          <svg viewBox="0 0 24 24" aria-hidden><path d="M4 5h16v11H9l-5 4zM8 9h8M8 12h5" /></svg>Agent 對話台
        </button>
        <button className={demoOpen ? 'on' : ''} aria-expanded={demoOpen} onClick={() => setDemoOpen((o) => !o)}>
          <svg viewBox="0 0 24 24" aria-hidden><path d="M8 5v14l11-7z" /></svg>展示控制
        </button>
      </nav>
    </div>
  )
}

export default function App() {
  return <WarRoomProvider><Dashboard /></WarRoomProvider>
}
