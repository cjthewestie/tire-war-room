// War room home 對話台: the 核准區 list, the 計劃板 of the selected item and the docked Agent conversation;
// 產線監控 (proposal page 2), 人力排程 (pages 4, 6, 11), 訂單 and 工廠全貌 are the other tabs (#monitor, #schedule, #orders, #world);
// tab order per the UX brief (CJ, 2026-10-20: 「產線監控跟人力排程是個別頁面」).
import { lazy, Suspense, useEffect, useState } from 'react'
import type { LocalItem } from './agent/transcript'
import { DemoStrip } from './components/DemoStrip'
import { Header, type Tab } from './components/Header'
import { MachineDrawer } from './components/MachineDrawer'
import { MonitorTab } from './components/MonitorTab'
import { ScheduleTab } from './components/ScheduleTab'
import { WorldTab } from './components/WorldTab'
import { OrdersTab } from './components/OrdersTab'
import { EarlyWarnings } from './components/EarlyWarnings'
import { TodayPlan } from './components/TodayPlan'
import { useWarRoom, WarRoomProvider } from './useWarRoom'

const AgentTab = lazy(() => import('./agent/AgentTab').then((m) => ({ default: m.AgentTab })))

function readTab(): Tab {
  const h = location.hash
  return h === '#schedule' ? 'schedule' : h === '#monitor' ? 'monitor' : h === '#orders' ? 'orders' : h === '#world' ? 'world' : 'agent'
}

function Dashboard() {
  const [tab, setTab] = useState<Tab>(readTab)
  const [machine, setMachine] = useState<string | null>(() => new URLSearchParams(location.search).get('machine'))
  const { state } = useWarRoom()
  useEffect(() => {
    const f = () => setTab(readTab())
    window.addEventListener('hashchange', f)
    return () => window.removeEventListener('hashchange', f)
  }, [])
  const go = (t: Tab) => { history.replaceState(null, '', t === 'agent' ? location.pathname + location.search : `${location.search}#${t}`); setTab(t) }
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
      <main>{tab === 'monitor' ? <><EarlyWarnings onMachine={setMachine} /><MonitorTab onMachine={setMachine} onAgent={openAgent} /></> : tab === 'schedule' ? <><TodayPlan /><ScheduleTab /></> : tab === 'orders' ? <OrdersTab /> : tab === 'world' ? <WorldTab />
        : <Suspense fallback={<div className="loading">載入對話台…</div>}><AgentTab asks={asks} setAsks={setAsks} picked={agentEvent} setPicked={setAgentEvent} onMonitor={() => go('monitor')} /></Suspense>}</main>
      {machine && <MachineDrawer id={machine} onClose={() => setMachine(null)} />}
      <footer className="foot-note">示意畫面；剩餘時間依硫化機產能與班別估算・物理與排程數值由公式與求解器計算，不由 AI 推測</footer>
      <nav className="bottomnav" aria-label="分頁">
        <button className={tab === 'agent' ? 'on' : ''} aria-current={tab === 'agent' ? 'page' : undefined} aria-label="對話台" onClick={() => go('agent')}>
          <svg viewBox="0 0 24 24" aria-hidden><path d="M4 5h16v11H9l-5 4zM8 9h8M8 12h5" /></svg>對話台{pendingN > 0 && <i className="nav-badge">{pendingN}</i>}
        </button>
        <button className={tab === 'monitor' ? 'on' : ''} aria-current={tab === 'monitor' ? 'page' : undefined} onClick={() => go('monitor')}>
          <svg viewBox="0 0 24 24" aria-hidden><path d="M3 13h4l3-8 4 14 3-6h4" /></svg>產線監控
        </button>
        <button className={tab === 'schedule' ? 'on' : ''} aria-current={tab === 'schedule' ? 'page' : undefined} aria-label="人力排程" onClick={() => go('schedule')}>
          <svg viewBox="0 0 24 24" aria-hidden><path d="M4 6h10M4 12h16M4 18h7" /></svg>人力排程
        </button>
        <button className={tab === 'orders' ? 'on' : ''} aria-current={tab === 'orders' ? 'page' : undefined} aria-label="訂單" onClick={() => go('orders')}>
          <svg viewBox="0 0 24 24" aria-hidden><circle cx="10.5" cy="10.5" r="6" /><path d="M15 15l5 5" /></svg>訂單
        </button>
        <button className={tab === 'world' ? 'on' : ''} aria-current={tab === 'world' ? 'page' : undefined} aria-label="工廠全貌" onClick={() => go('world')}>
          <svg viewBox="0 0 24 24" aria-hidden><path d="M3 20V9l5 3V9l5 3V5h4l1 15zM3 20h18" /></svg>工廠全貌
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
