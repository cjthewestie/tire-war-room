// UX declutter: on a phone the secondary blocks of 產線監控 and 人力排程 fold into their headers (native <details>),
// while desktop keeps everything open. The breakpoint is the repo's phone one (index.css `max-width: 599px`).
import { useCallback, useSyncExternalStore } from 'react'

export const PHONE_QUERY = '(max-width: 599px)'

export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback((cb: () => void) => {
    const mq = window.matchMedia(query)
    mq.addEventListener('change', cb)
    return () => mq.removeEventListener('change', cb)
  }, [query])
  return useSyncExternalStore(subscribe, () => window.matchMedia(query).matches, () => false)
}

/** Open every folded block around `el`, so a jump to a machine inside a folded panel lands on something visible. */
export function openFolds(el: Element | null) {
  for (let d = el?.closest('details'); d; d = d.parentElement?.closest('details')) d.open = true
}
