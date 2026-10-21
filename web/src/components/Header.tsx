import { machineLabel } from '../factory'
import { fmtHM, fmtMD } from '../format'
import { QUALITY_LABEL } from './common'
import { useLinkStatus } from '../linkStatus'
import { useWarRoom } from '../useWarRoom'

export type Tab = 'monitor' | 'schedule' | 'orders' | 'console' | 'sm' | 'admin' | 'agent'

const NET_LABEL = { online: '網路正常', weak: '弱網・延後推播（Agent 不受影響）', wan_down: 'WAN 中斷・手機走廠內 LAN', lan_down: 'LAN 中斷・現場觸控螢幕＋警示燈' }

export function Header({ tab, onTab }: { tab: Tab; onTab: (t: Tab) => void }) {
  const { state, transport, connected } = useWarRoom()
  const link = useLinkStatus()
  const h = state.snapshot?.header
  const ft = state.clock?.factory_time ?? h?.factory_time
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
        <button role="tab" aria-selected={tab === 'console'} className={tab === 'console' ? 'on' : ''} onClick={() => onTab('console')} data-testid="tab-console" title="Agent 完整過程：狀態、計畫、工具、資料、把關、核准">Agent</button>
        <button role="tab" aria-selected={tab === 'sm'} className={tab === 'sm' ? 'on' : ''} onClick={() => onTab('sm')} data-testid="tab-sm" title="狀態機：事件偵測、各狀態的即時看板、每日事件紀錄">狀態機</button>
        <button role="tab" aria-selected={tab === 'admin'} className={tab === 'admin' ? 'on' : ''} onClick={() => onTab('admin')} data-testid="tab-admin" title="後台：RAG 知識庫、記憶、狀態、執行紀錄（主管 PIN・限廠內網路）">後台</button>
      </nav>
      <div className="top-right">
        {net !== 'online' && <span className={`chip net ${net}`} title="離線分層（p.9）">{NET_LABEL[net]}</span>}
        {h?.warning_light && <span className="chip bad" data-testid="warning-light" title="LAN 中斷：請至現場觸控螢幕處理"><i className="dot bad" />警示燈亮</span>}
        {(h?.bad_data_total ?? 0) > 0 && (
          <span className="chip data" data-testid="chip-bad-data"
            title={(h?.bad_data ?? []).map((b) => `${machineLabel(b.machine_id)}：${b.count} 筆（${QUALITY_LABEL[b.last_quality] ?? b.last_quality}）`).join('\n') + '\n近 30 分；壞資料不喚醒 AI（p.10）'}>
            <i className="dot data" />驗證層攔下 {h?.bad_data_total} 筆壞資料（{(h?.bad_data ?? []).map((b) => machineLabel(b.machine_id)).join('、')}・未喚醒 AI）
          </span>
        )}
        {/* one chip for both counts (UX declutter); it turns red while any machine is abnormal */}
        <span className={`chip ${h && h.abnormal_count > 0 ? 'bad' : 'ok'}`} data-testid="chip-counts">
          <i className={`dot ${h && h.abnormal_count > 0 ? 'bad' : 'ok'}`} />
          <span data-testid="chip-normal">正常 {h?.normal_count ?? '–'} 台</span>・<span data-testid="chip-abnormal">異常 {h?.abnormal_count ?? '–'} 台</span>
        </span>
        <span className="clock">{ft ? <><span>{fmtMD(ft)}</span><span>{fmtHM(ft)}</span></> : '—'}</span>
        {/* README: 「畫面時間旁顯示一日循環・第 n 輪」 — kept beside the time, small and muted; full wording in the tooltip */}
        {state.clock?.day_loop && (
          <span className="clock-loop" data-testid="chip-loop" title={`一日循環・第 ${state.clock.day_loop.loop} 輪：團隊資料集 ${state.clock.day_loop.source_day} 一整天，24:00 回到 00:00 重跑`}>
            <span className="sr">一日循環・</span>第 {state.clock.day_loop.loop} 輪
          </span>
        )}
        {/* .live = connected to the backend (e2e waits on it); tone: ok | warn | mock | down */}
        <span className={`mode t-${link.tone} ${transport?.mode === 'live' && connected ? 'live' : ''} ${transport?.mode === 'mock' ? 'mock' : ''}`} title={link.title} data-testid="link-chip" role="status">
          {link.text}
        </span>
      </div>
    </header>
  )
}
