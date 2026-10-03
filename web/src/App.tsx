// War-room dashboard (proposal page 2) + 排程與人力 tab (pages 4, 6, 11).
import { useEffect, useState } from 'react'
import { DemoStrip } from './components/DemoStrip'
import { Header, type Tab } from './components/Header'
import { MachineDrawer } from './components/MachineDrawer'
import { MonitorTab } from './components/MonitorTab'
import { ScheduleTab } from './components/ScheduleTab'
import { useWarRoom, WarRoomProvider } from './useWarRoom'

function readTab(): Tab { return location.hash === '#schedule' ? 'schedule' : 'monitor' }

function Dashboard() {
  const [tab, setTab] = useState<Tab>(readTab)
  const [machine, setMachine] = useState<string | null>(() => new URLSearchParams(location.search).get('machine'))
  const { state } = useWarRoom()
  useEffect(() => {
    const f = () => setTab(readTab())
    window.addEventListener('hashchange', f)
    return () => window.removeEventListener('hashchange', f)
  }, [])
  const go = (t: Tab) => { history.replaceState(null, '', t === 'schedule' ? `${location.search}#schedule` : location.pathname + location.search); setTab(t) }
  const lanDown = state.snapshot?.header.network === 'lan_down'
  return (
    <div className={`app ${lanDown ? 'lan-down' : ''}`}>
      <Header tab={tab} onTab={go} />
      <DemoStrip />
      {lanDown && <div className="banner">LAN 中斷：手機連不上，核准改在現場觸控螢幕操作，警示燈已亮。AI 與安全核心照常在邊緣主機運作。</div>}
      <main>{tab === 'monitor' ? <MonitorTab onMachine={setMachine} /> : <ScheduleTab />}</main>
      {machine && <MachineDrawer id={machine} onClose={() => setMachine(null)} />}
      <footer className="foot-note">示意畫面；剩餘時間依硫化機產能與班別估算・物理與排程數值由公式與求解器計算，不由 AI 推測</footer>
    </div>
  )
}

export default function App() {
  return <WarRoomProvider><Dashboard /></WarRoomProvider>
}
