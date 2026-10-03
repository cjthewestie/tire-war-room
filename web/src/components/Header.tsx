import { fmtHM, fmtMD } from '../format'
import { useWarRoom } from '../useWarRoom'

export type Tab = 'monitor' | 'schedule'

const NET_LABEL = { online: '網路正常', wan_down: 'WAN 中斷・手機走廠內 LAN', lan_down: 'LAN 中斷・現場觸控螢幕＋警示燈' }

export function Header({ tab, onTab }: { tab: Tab; onTab: (t: Tab) => void }) {
  const { state, transport, connected } = useWarRoom()
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
        <button role="tab" aria-selected={tab === 'monitor'} className={tab === 'monitor' ? 'on' : ''} onClick={() => onTab('monitor')}>產線監控</button>
        <button role="tab" aria-selected={tab === 'schedule'} className={tab === 'schedule' ? 'on' : ''} onClick={() => onTab('schedule')}>排程與人力</button>
      </nav>
      <div className="top-right">
        {net !== 'online' && <span className={`chip net ${net}`} title="離線分層（p.9）">{NET_LABEL[net]}</span>}
        {h?.warning_light && <span className="chip bad" data-testid="warning-light" title="LAN 中斷：請至現場觸控螢幕處理"><i className="dot bad" />警示燈亮</span>}
        <span className="chip ok" data-testid="chip-normal"><i className="dot ok" />正常 {h?.normal_count ?? '–'} 台</span>
        <span className={`chip ${h && h.abnormal_count > 0 ? 'bad' : 'muted'}`} data-testid="chip-abnormal"><i className={`dot ${h && h.abnormal_count > 0 ? 'bad' : 'idle'}`} />異常 {h?.abnormal_count ?? '–'} 台</span>
        <span className="clock">{ft ? <><span>{fmtMD(ft)}</span><span>{fmtHM(ft)}</span></> : '—'}</span>
        <span className={`mode ${transport?.mode ?? ''} ${connected ? '' : 'down'}`} title={transport?.mode === 'mock' ? '瀏覽器內模擬資料（無後端）' : '連線 FastAPI /ws'}>
          {transport ? (transport.mode === 'mock' ? '模擬資料' : connected ? `即時・${h?.llm_mode === 'llm' ? 'LLM 指揮官' : '規則模式指揮官'}` : '重新連線中…') : '連線中…'}
        </span>
      </div>
    </header>
  )
}
