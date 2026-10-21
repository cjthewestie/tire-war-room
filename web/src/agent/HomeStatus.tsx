// 主控台 status line as a 產線監控 ticker (CJ, 2026-10-20: 「幫我設計成產線監控的跑馬燈好了」). One full-width row (issue #100:
// the 正常 / 異常 / 待核准 count bar is gone; the counts are the KPI cards in 今日工廠概況): a scrolling ticker of live
// plant items (tickerItems.ts); alarm / warning items are pinned first in their colour and also scroll (on a phone
// the pinned ones take the first line, so 異常 / 警告 come first). Hover or keyboard focus pauses it; an item
// opens 產線監控 on its machine or line. prefers-reduced-motion: no scrolling, the items wrap as a static row.
// A new order completion / shipment shows a short success toast. data-testid=status-strip is kept for the e2e checks.
import { useEffect, useMemo, useState } from 'react'
import type { Line, Shift } from '../contracts'
import { ms } from '../format'
import { useWarRoom } from '../useWarRoom'
import { successOf, successText, tickerItems, type Success, type TickItem } from './tickerItems'

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
  const open = (i: TickItem) => (onFocus ? onFocus(i.machine, i.line) : onMonitor?.())
  return (
    <div className="tk" data-testid="status-strip" aria-label="產線監控跑馬燈">
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
