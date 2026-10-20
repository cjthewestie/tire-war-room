// 主控台 status line as a 產線監控 ticker (CJ, 2026-10-20: 「幫我設計成產線監控的跑馬燈好了」). Left, fixed: 正常 / 異常 /
// 待核准 counts (they never move; click → 產線監控). Right: a scrolling ticker of live plant items (tickerItems.ts);
// alarm / warning items are pinned first in their colour and also scroll. Hover or keyboard focus pauses it; an item
// opens 產線監控 on its machine or line. prefers-reduced-motion: no scrolling, the items wrap as a static row.
// A new order completion / shipment shows a short success toast. data-testid=status-strip is kept for the e2e checks.
import { useEffect, useMemo, useState } from 'react'
import type { Line, Shift } from '../contracts'
import { ms } from '../format'
import { useWarRoom } from '../useWarRoom'
import { ThemeToggle } from '../components/ThemeToggle'
import { successOf, successText, tickerItems, type Success, type TickItem } from './tickerItems'

const NET_SHORT: Record<string, string> = { online: '網路正常', weak: '弱網', wan_down: 'WAN 中斷', lan_down: 'LAN 中斷・現場操作' }
const H = 3_600_000

export function HomeStatus({ onMonitor, onFocus }: { onMonitor?: () => void; onFocus?: (machine?: string, line?: Line) => void }) {
  const { state, transport } = useWarRoom()
  const h = state.snapshot?.header
  const now = ms(state.clock?.factory_time ?? h?.factory_time)
  const day = isNaN(now) ? 0 : Math.floor(now / (6 * H))
  const [shifts, setShifts] = useState<Shift[]>([])
  useEffect(() => { transport?.shifts().then(setShifts).catch(() => {}) }, [transport, day])
  const minute = isNaN(now) ? 0 : Math.floor(now / 60_000)
  const items = useMemo(() => tickerItems(state.snapshot, state.schedule, state.world, shifts, minute * 60_000),
    [state.snapshot, state.schedule, state.world, shifts, minute])
  const pinned = items.filter((i) => i.tone === 'bad' || i.tone === 'warn')
  const pend = state.pending.length
  const net = h?.network ?? 'online'
  const abn = h?.abnormal_count ?? 0
  const open = (i: TickItem) => (onFocus ? onFocus(i.machine, i.line) : onMonitor?.())
  return (
    <div className="tk" data-testid="status-strip" aria-label="產線監控跑馬燈">
      <button type="button" className="tk-fixed" onClick={onMonitor} title="打開產線監控" data-testid="tk-fixed">
        <span className="as-item ok"><i className="dot ok" />正常 <b>{h?.normal_count ?? '–'}</b></span>
        <span className={`as-item ${abn > 0 ? 'bad' : ''}`}><i className={`dot ${abn > 0 ? 'bad' : 'idle'}`} />異常 <b>{abn}</b></span>
        <span className={`as-item ${pend ? 'warn' : ''}`}>待核准 <b>{pend}</b></span>
        {net !== 'online' && <span className="as-item bad">{NET_SHORT[net] ?? net}</span>}
      </button>
      <div className="tk-run" role="region" aria-label="即時產線動態（滑鼠移上去暫停）">
        {pinned.length > 0 && (
          <div className="tk-pinned" data-testid="tk-pinned">
            {pinned.map((i) => <Tick key={`p-${i.key}`} i={i} onClick={open} />)}
          </div>
        )}
        <div className="tk-view">
          <div className="tk-track" style={{ animationDuration: `${Math.max(20, items.length * 3.2)}s` }} data-testid="tk-track">
            {items.map((i) => <Tick key={i.key} i={i} onClick={open} />)}
            <span className="tk-dup" aria-hidden>{items.map((i) => <Tick key={`d-${i.key}`} i={i} onClick={open} dup />)}</span>
          </div>
        </div>
      </div>
      <ThemeToggle />
      <SuccessToast />
    </div>
  )
}

function Tick({ i, onClick, dup = false }: { i: TickItem; onClick: (i: TickItem) => void; dup?: boolean }) {
  return (
    <button type="button" className={`tk-i t-${i.tone}`} onClick={() => onClick(i)} tabIndex={dup ? -1 : undefined} data-tick={dup ? undefined : i.key}
      title={i.machine || i.line ? '在產線監控打開' : '打開產線監控'}>{i.text}</button>
  )
}

/** A short toast for each order completion / shipment that happens while the page is open (not for old ones). */
function SuccessToast() {
  const { state } = useWarRoom()
  const evs = state.world?.events
  const [seen, setSeen] = useState<Set<string> | null>(null)
  const [toast, setToast] = useState<Success | null>(null)
  const all = useMemo(() => (evs ?? []).map(successOf).filter((x): x is Success => !!x), [evs])
  if (evs && seen === null) setSeen(new Set(all.map((s) => s.key)))
  else if (seen) {
    const fresh = all.find((s) => !seen.has(s.key))
    if (fresh) { setSeen(new Set([...seen, ...all.map((s) => s.key)])); setToast(fresh) }
  }
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 6000)
    return () => clearTimeout(t)
  }, [toast])
  if (!toast) return null
  return <div className={`tk-toast ${toast.onTime === false ? 'late' : ''}`} role="status" aria-live="polite" data-testid="success-toast">{successText(toast)}</div>
}
