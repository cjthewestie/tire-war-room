// War room home 主控台 (was 對話台; CJ, 2026-10-20: 「這個 page 改成主控台名字」): the 核准區 list, the 計劃板 of the selected item and the docked Agent dialogue;
// 產線監控 (proposal page 2 + the whole plant, which was 工廠全貌), 人力排程 (pages 4, 6, 11), 訂單, Agent (the full
// transcript), 狀態機 (live board + 每日事件紀錄) and 後台 are the other tabs (#monitor, #schedule, #orders, #console, #sm, #admin); #world still opens 產線監控.
// CJ, 2026-10-20: 「產線監控跟人力排程是個別頁面」「產線監控跟工廠全貌…重複到了…可以合併一下」.
import { lazy, Suspense, useEffect, useState } from 'react'
import type { Line } from './contracts'
import type { LocalItem } from './agent/transcript'
import { DemoStrip } from './components/DemoStrip'
import { Header, type Tab } from './components/Header'
import { MachineDrawer } from './components/MachineDrawer'
import { MonitorTab } from './components/MonitorTab'
import { ScheduleTab } from './components/ScheduleTab'
import { OrdersTab } from './components/OrdersTab'
import { EarlyWarnings } from './components/EarlyWarnings'
import { TodayPlan } from './components/TodayPlan'
import { useWarRoom, WarRoomProvider } from './useWarRoom'
import { readConsoleEvent } from './agent/consoleHash'

const AgentTab = lazy(() => import('./agent/AgentTab').then((m) => ({ default: m.AgentTab })))
const AgentConsolePage = lazy(() => import('./agent/AgentConsolePage').then((m) => ({ default: m.AgentConsolePage })))
const StateMachinePage = lazy(() => import('./statemachine/StateMachinePage').then((m) => ({ default: m.StateMachinePage })))
const AdminPage = lazy(() => import('./admin/AdminPage').then((m) => ({ default: m.AdminPage })))

function readTab(): Tab {
  const h = `#${/^#(\w+)/.exec(location.hash)?.[1] ?? ''}`
  return h === '#schedule' ? 'schedule' : h === '#monitor' || h === '#world' ? 'monitor' : h === '#orders' ? 'orders'
    : h === '#console' ? 'console' : h === '#sm' ? 'sm' : h === '#admin' ? 'admin' : 'agent'
}

function Dashboard() {
  const [tab, setTab] = useState<Tab>(readTab)
  const [consoleEvent, setConsoleEvent] = useState<string | null>(readConsoleEvent)
  const [machine, setMachine] = useState<string | null>(() => new URLSearchParams(location.search).get('machine'))
  const { state } = useWarRoom()
  useEffect(() => {
    const f = () => { setTab(readTab()); setConsoleEvent(readConsoleEvent()) }
    window.addEventListener('hashchange', f)
    return () => window.removeEventListener('hashchange', f)
  }, [])
  const go = (t: Tab, sub?: string) => {
    history.replaceState(null, '', t === 'agent' ? location.pathname + location.search : `${location.search}#${t}${sub ?? ''}`)
    setTab(t); window.scrollTo(0, 0)
  }
  const [asks, setAsks] = useState<LocalItem[]>([])
  const [agentEvent, setAgentEvent] = useState<string | null>(null)
  const openAgent = (eventId?: string) => { if (eventId) setAgentEvent(eventId); go('agent') }
  // a ticker item: 產線監控 on that machine (its drawer) or line, scrolled into view and flashed
  const focusPlant = (m?: string, line?: Line) => {
    go('monitor')
    if (m) setMachine(m)
    setTimeout(() => {
      const el = document.querySelector(m ? `[data-machine="${m}"]` : line ? `[data-line="${line}"]` : '.machines')
      el?.scrollIntoView({ block: 'center' })
      el?.classList.add('flash'); setTimeout(() => el?.classList.remove('flash'), 1600)
    }, 120)
  }
  const openConsole = (eventId?: string) => { setConsoleEvent(eventId ?? null); go('console', eventId ? `?event=${encodeURIComponent(eventId)}` : '') }
  const [demoOpen, setDemoOpen] = useState(false)
  const [moreOpen, setMoreOpen] = useState(false)   // phones: 狀態機 and 後台 live under 「更多」 (issue #52)
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
      <main>{tab === 'monitor' ? <><EarlyWarnings onMachine={setMachine} /><MonitorTab onMachine={setMachine} onAgent={openAgent} onOrders={() => go('orders')} /></>
        : tab === 'schedule' ? <><TodayPlan /><ScheduleTab /></> : tab === 'orders' ? <OrdersTab />
        : tab === 'console' ? <Suspense fallback={<div className="loading">載入 Agent…</div>}><AgentConsolePage asks={asks} setAsks={setAsks} initial={consoleEvent} /></Suspense>
        : tab === 'sm' ? <Suspense fallback={<div className="loading">載入狀態機…</div>}><StateMachinePage /></Suspense>
        : tab === 'admin' ? <Suspense fallback={<div className="loading">載入後台…</div>}><AdminPage /></Suspense>
        : <Suspense fallback={<div className="loading">載入主控台…</div>}><AgentTab asks={asks} setAsks={setAsks} picked={agentEvent} setPicked={setAgentEvent} onMonitor={() => go('monitor')} onFull={openConsole} onFocus={focusPlant} /></Suspense>}</main>
      {machine && <MachineDrawer id={machine} onClose={() => setMachine(null)} />}
      <footer className="foot-note">示意畫面；剩餘時間依硫化機產能與班別估算・物理與排程數值由公式與求解器計算，不由 AI 推測</footer>
      <nav className="bottomnav" aria-label="分頁">
        <button className={tab === 'agent' ? 'on' : ''} aria-current={tab === 'agent' ? 'page' : undefined} aria-label="主控台" onClick={() => go('agent')}>
          <svg viewBox="0 0 24 24" aria-hidden><path d="M4 5h16v11H9l-5 4zM8 9h8M8 12h5" /></svg>主控台{pendingN > 0 && <i className="nav-badge">{pendingN}</i>}
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
        <button className={tab === 'console' ? 'on' : ''} aria-current={tab === 'console' ? 'page' : undefined} aria-label="Agent 完整過程" onClick={() => go('console')}>
          <svg viewBox="0 0 24 24" aria-hidden><path d="M4 5h16v14H4zM7 9l3 3-3 3M12 15h5" /></svg>Agent
        </button>
        <button className={tab === 'sm' || tab === 'admin' || moreOpen ? 'on' : ''} aria-expanded={moreOpen} aria-haspopup="menu" aria-label="更多：狀態機、後台" onClick={() => setMoreOpen((o) => !o)} data-testid="nav-more">
          <svg viewBox="0 0 24 24" aria-hidden><circle cx="5" cy="12" r="1.6" /><circle cx="12" cy="12" r="1.6" /><circle cx="19" cy="12" r="1.6" /></svg>{tab === 'sm' ? '狀態機' : tab === 'admin' ? '後台' : '更多'}
        </button>
        <button className={demoOpen ? 'on' : ''} aria-expanded={demoOpen} onClick={() => setDemoOpen((o) => !o)}>
          <svg viewBox="0 0 24 24" aria-hidden><path d="M8 5v14l11-7z" /></svg>展示控制
        </button>
      </nav>
      {moreOpen && (
        <>
          <div className="demo-scrim" onClick={() => setMoreOpen(false)} aria-hidden />
          <div className="more-sheet" role="menu" aria-label="更多" data-testid="more-sheet">
            <button role="menuitem" className={tab === 'sm' ? 'on' : ''} onClick={() => { setMoreOpen(false); go('sm') }}>
              <svg viewBox="0 0 24 24" aria-hidden><circle cx="5" cy="6" r="2.5" /><circle cx="19" cy="6" r="2.5" /><circle cx="12" cy="18" r="2.5" /><path d="M7.5 6h9M17.6 8.2l-4.4 7.6M10.8 15.8 6.4 8.2" /></svg>
              <span><b>狀態機</b><small>事件偵測、各狀態的即時看板、每日事件紀錄</small></span>
            </button>
            <button role="menuitem" className={tab === 'admin' ? 'on' : ''} onClick={() => { setMoreOpen(false); go('admin') }}>
              <svg viewBox="0 0 24 24" aria-hidden><path d="M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2" /></svg>
              <span><b>後台</b><small>RAG 知識庫、工廠指示、記憶、狀態（主管 PIN）</small></span>
            </button>
          </div>
        </>
      )}
    </div>
  )
}

export default function App() {
  return <WarRoomProvider><Dashboard /></WarRoomProvider>
}
