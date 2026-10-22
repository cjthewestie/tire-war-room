// 「⚠ 待核准 N 件」 on every war-room page while cards wait (CJ, 2026-10-22: 「核准區有代辦時應該全部頁面都要看得到告警」):
// a sticky slim bar under the header (war-room tabs), compact on 主控台 and during the Demo spotlight, inline in the
// sticky header of /report and /handover, a corner overlay on /factory. Not on /phone (its own approval UI and title)
// nor /worker (workers do not approve). The whole bar is one click target: the oldest card on 主控台.
import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react'
import { alertModel, approveHref, fmtClock, fmtWaited, titleWithPending } from '../approvalAlert'
import { ms } from '../format'
import { embedSearch } from '../transport'
import { useWarRoom } from '../useWarRoom'

export type AlertVariant = 'bar' | 'compact' | 'inline' | 'corner'

export function PendingApprovalAlert({ variant = 'bar', onGo, newTab = false, stickyBelow }: {
  variant?: AlertVariant
  /** in-app jump (war room); without it the bar is a link to `/#approve?event=<id>` */
  onGo?: (eventId: string) => void
  newTab?: boolean
  /** selector of a sticky header the bar sits under (its height while it is sticky, 0 when it is not) */
  stickyBelow?: string
}) {
  const { state } = useWarRoom()
  const n = state.pending.length
  usePendingTitle(n)
  const top = useStickyTop(stickyBelow, n > 0)
  const wrap = useRef<HTMLDivElement>(null)
  usePublishChrome(stickyBelow, wrap, variant, n > 0)

  // the server sends the clock every 0.5 s (runtime.py), so the countdown ticks with it; paused = it stops
  const m = alertModel(state.pending, ms(state.clock?.factory_time ?? state.snapshot?.header.factory_time), state.clock?.speed ?? 1)
  if (!m) return null
  const left = m.leftS != null ? `${fmtClock(m.leftS)} 後逾時` : null
  const cls = `paa paa-${variant} ${m.red ? 'red' : 'amber'} ${m.pulse ? 'pulse' : ''}`
  const label = `待核准 ${m.n} 件，最久一件「${m.title}」${fmtWaited(m.waitedS)}${left ? `，${left}` : ''}。去核准`
  const body = (
    <>
      <b className="paa-n"><span aria-hidden>⚠ </span>待核准 {m.n} 件</b>
      <span className="paa-t" title={m.title}>{m.title}</span>
      <span className="paa-w">{fmtWaited(m.waitedS)}</span>
      {left && <span className="paa-left" data-testid="pending-alert-left">{left}</span>}
      <span className="paa-go">去核准 →</span>
    </>
  )
  return (
    <div ref={wrap} className={`paa-wrap paa-wrap-${variant} no-print`} style={top != null ? { top: `calc(${top}px + var(--demo-bar-h, 0px))` } : undefined} data-testid="pending-alert"
      data-event={m.oldest.event_id} data-level={m.red ? 'red' : 'amber'}>
      {onGo
        ? <button type="button" className={cls} aria-label={label} onClick={() => onGo(m.oldest.event_id)}>{body}</button>
        : <a className={cls} aria-label={label} href={approveHref(m.oldest.event_id, embedSearch())}
          {...(newTab ? { target: '_blank', rel: 'noopener' } : {})}>{body}</a>}
    </div>
  )
}

/** 「(N) 待核准・」 before the page's own title while N > 0; the page's title comes back at 0 or on unmount. */
function usePendingTitle(n: number) {
  const base = useRef<string | null>(null)
  useEffect(() => {
    if (base.current == null) base.current = document.title
    document.title = titleWithPending(base.current, n)
  }, [n])
  useEffect(() => () => { if (base.current != null) document.title = base.current }, [])
}

/** The bar sticks right under the war-room header while that header is sticky (it is static on short landscape). */
function useStickyTop(selector: string | undefined, on: boolean): number | null {
  const [top, setTop] = useState<number | null>(null)
  useLayoutEffect(() => {
    if (!selector || !on) return
    const el = document.querySelector<HTMLElement>(selector)
    if (!el) return
    const f = () => setTop(getComputedStyle(el).position === 'sticky' ? el.offsetHeight : 0)
    f()
    const ro = new ResizeObserver(f)
    ro.observe(el)
    window.addEventListener('resize', f)
    return () => { ro.disconnect(); window.removeEventListener('resize', f) }
  }, [selector, on])
  return top
}

/** Publishes the sticky chrome heights as CSS variables so sticky columns and the Demo's scrolling stop below them:
 *  --topbar-h = the header (0 while it is static), --paa-h = this bar while it is sticky in the page (0 otherwise).
 *  Layout measurement only. */
function usePublishChrome(selector: string | undefined, wrap: RefObject<HTMLDivElement | null>, variant: AlertVariant, on: boolean) {
  useLayoutEffect(() => {
    if (!selector) return
    const root = document.documentElement
    const hdr = document.querySelector<HTMLElement>(selector)
    const bar = on && variant !== 'inline' && variant !== 'corner' ? wrap.current : null
    const f = () => {
      root.style.setProperty('--topbar-h', `${hdr && getComputedStyle(hdr).position === 'sticky' ? hdr.offsetHeight : 0}px`)
      root.style.setProperty('--paa-h', `${bar && getComputedStyle(bar).position === 'sticky' ? bar.offsetHeight : 0}px`)
    }
    f()
    const ro = new ResizeObserver(f)
    if (hdr) ro.observe(hdr)
    if (bar) ro.observe(bar)
    window.addEventListener('resize', f)
    return () => {
      ro.disconnect(); window.removeEventListener('resize', f)
      root.style.removeProperty('--topbar-h'); root.style.removeProperty('--paa-h')
    }
  }, [selector, wrap, variant, on])
}
