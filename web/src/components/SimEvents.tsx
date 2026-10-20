// 模擬事件 (issue #36, CJ 2026-10-20 「我想要有模擬事件」): the 隨機事件 switch and the 觸發事件 ▾ menu for the demo strip.
// Live only: the server's generator (warroom/sim/events.py) draws one weighted event every N factory minutes and
// sends it down the normal path (machine faults through the detector, plant events through the state machine,
// writes through the gate). Its state comes in every WS clock message, so the dashboard, the 3D desk and key 4 agree.
import { useRef } from 'react'
import { useSimEvents } from './useSimEvents'

const EVERY = [10, 30, 60]

export function SimEventControls({ busy, run }: { busy: boolean; run: (fn: () => Promise<unknown>) => Promise<void> }) {
  const ev = useSimEvents()
  const menu = useRef<HTMLDetailsElement>(null)
  if (!ev.live) return null
  const on = !!ev.rnd?.enabled
  return (<>
    <div className="seg sim-ev" role="group" aria-label="模擬事件" data-testid="sim-events">
      <button disabled={busy} className={on ? 'on ev-on' : ''} aria-pressed={on} data-testid="sim-events-toggle"
        title={`每 ${ev.rnd?.every_min ?? 30} 個模擬分鐘，從加權清單抽一件事件（機台故障走事件偵測；插單、病假、原料走工廠事件），一律經狀態機與閘門。種子 ${ev.rnd?.seed ?? '—'}`}
        onClick={() => run(() => ev.toggle())}>{on ? '● 隨機事件' : '○ 隨機事件'}</button>
      {on && EVERY.map((m) => (
        <button key={m} disabled={busy} className={ev.rnd?.every_min === m ? 'on' : ''} onClick={() => run(() => ev.setEvery(m))}>每 {m} 分</button>
      ))}
      <details className="sim-ev-menu" ref={menu}>
        <summary data-testid="sim-events-menu">觸發事件 ▾</summary>
        <div className="sim-ev-list" role="menu">
          {ev.catalog.map((c) => (
            <button key={c.kind} role="menuitem" disabled={busy} data-kind={c.kind}
              onClick={() => { if (menu.current) menu.current.open = false; void run(() => ev.fire(c.kind)) }}>
              {c.label}<small>{c.source === 'fault' ? '機台' : c.source === 'world' ? '原料' : '工廠'}</small>
            </button>
          ))}
        </div>
      </details>
    </div>
    {ev.note && <span className="sim-ev-note" role="status" data-testid="sim-events-note">{ev.note}</span>}
  </>)
}
