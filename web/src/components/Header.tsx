import { useEffect, useRef, useState } from 'react'
import { fmtHM, fmtMD, fmtWeekdayShort } from '../format'
import { ThemeToggle } from './ThemeToggle'
import { useLinkStatus } from '../linkStatus'
import { useWarRoom } from '../useWarRoom'

export type Tab = 'monitor' | 'schedule' | 'orders' | 'console' | 'sm' | 'admin' | 'qa' | 'agent'

const NET_LABEL = { online: '網路正常', weak: '網路不穩・手機通知會延後', wan_down: '外網斷線・手機改走廠內網路（LAN）', lan_down: '廠內網路斷線・請到現場觸控螢幕核准' }

export function Header({ tab, onTab }: { tab: Tab; onTab: (t: Tab) => void }) {
  const { state, transport, connected } = useWarRoom()
  const link = useLinkStatus()
  const h = state.snapshot?.header
  const net = h?.network ?? 'online'
  return (
    <header className="topbar">
      <div className="brand">
        <span className="logo" aria-hidden>◆</span>
        <h1>實心胎智慧製造戰情室</h1>
      </div>
      <nav className="tabs" role="tablist">
        <button role="tab" aria-selected={tab === 'agent'} className={tab === 'agent' ? 'on' : ''} onClick={() => onTab('agent')} data-testid="tab-agent">主控台</button>
        <button role="tab" aria-selected={tab === 'monitor'} className={tab === 'monitor' ? 'on' : ''} onClick={() => onTab('monitor')} data-testid="tab-monitor">產線監控</button>
        <button role="tab" aria-selected={tab === 'schedule'} className={tab === 'schedule' ? 'on' : ''} onClick={() => onTab('schedule')}>人力排程</button>
        <button role="tab" aria-selected={tab === 'orders'} className={tab === 'orders' ? 'on' : ''} onClick={() => onTab('orders')} data-testid="tab-orders">訂單</button>
        <button role="tab" aria-selected={tab === 'console'} className={tab === 'console' ? 'on' : ''} onClick={() => onTab('console')} data-testid="tab-console" title="Agent Nemo 完整過程：狀態、計畫、工具、資料、安全把關、核准">Agent Nemo</button>
        <button role="tab" aria-selected={tab === 'sm'} className={tab === 'sm' ? 'on' : ''} onClick={() => onTab('sm')} data-testid="tab-sm" title="狀態機：事件偵測、各狀態的即時看板、每日事件紀錄">狀態機</button>
        <button role="tab" aria-selected={tab === 'admin'} className={tab === 'admin' ? 'on' : ''} onClick={() => onTab('admin')} data-testid="tab-admin" title="後台：SOP 知識庫、記憶、工廠指示、系統狀態、執行紀錄（限廠內網路，要主管 PIN）">後台</button>
      </nav>
      <div className="top-right">
        {net !== 'online' && <span className={`chip net ${net}`} title={NET_LABEL[net]}>{NET_LABEL[net]}</span>}
        {h?.warning_light && <span className="chip bad" data-testid="warning-light" title="廠內網路斷線：請到現場觸控螢幕核准"><i className="dot bad" />警示燈亮</span>}
        {/* readings the validation layer rejected are listed in 預警 and on 狀態機, not here: the header stays one row */}
        <RealClock />
        <UserMenu />
        <ThemeToggle />
        {/* .live = connected to the backend (e2e waits on it); tone: ok | warn | mock | down */}
        <span className={`mode t-${link.tone} ${transport?.mode === 'live' && connected ? 'live' : ''} ${transport?.mode === 'mock' ? 'mock' : ''}`} title={link.title} data-testid="link-chip" role="status">
          {link.text}
        </span>
      </div>
    </header>
  )
}

/** The real clock (Taipei), no label (CJ, 2026-10-22). The plant's data runs on the simulated factory time, which is
 *  labelled 模擬工廠時間 where it is shown (今日工廠概況). On a phone the date is the short 10/21 so it never wraps. */
function RealClock() {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 10_000); return () => clearInterval(t) }, [])
  const full = `${fmtMD(now)}（${fmtWeekdayShort(now)}）${fmtHM(now)}`
  const short = `${fmtMD(now).slice(5)} ${fmtHM(now)}`
  return (
    <span className="clock-wrap">
      <span className="clock" data-testid="clock" title="現在的日期與時間（台北）">
        <span className="clock-full">{full}</span><span className="clock-short" aria-hidden>{short}</span>
      </span>
    </span>
  )
}

/** 登入身分: the page is shown as already signed in. A sketch only: no sign-in or sign-out is wired up, so no
 *  sign-out button is shown (Kyle 10-22: an unfinished button should not be on screen). */
function UserMenu() {
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const off = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) setOpen(false) }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', off); document.addEventListener('keydown', esc)
    return () => { document.removeEventListener('mousedown', off); document.removeEventListener('keydown', esc) }
  }, [open])
  return (
    <div className="usermenu" ref={box}>
      <button type="button" className="user-btn" aria-expanded={open} onClick={() => setOpen((o) => !o)} data-testid="user-menu">
        <span className="user-av" aria-hidden>主</span><span className="user-name">主管</span><span className="user-caret" aria-hidden>▾</span>
      </button>
      {open && (
        <div className="user-pop">
          <div className="user-who"><b>主管</b><small>已登入</small></div>
        </div>
      )}
    </div>
  )
}
