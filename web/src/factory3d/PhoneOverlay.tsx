// The raised supervisor phone: the real /phone page in an iframe, framed as a phone held in the right hand.
// Same origin, so approve / deny and 問戰情室 go through the phone page's own code and the same API.
import { useEffect, useRef } from 'react'

export function PhoneOverlay({ up, onLower, buzzing }: { up: boolean; onLower: () => void; buzzing: boolean }) {
  const frame = useRef<HTMLIFrameElement>(null)
  const lower = useRef(onLower)
  useEffect(() => { lower.current = onLower }, [onLower])

  // Keys typed inside the phone stay in the iframe: listen there for Esc (and P outside text fields).
  const hook = () => {
    try {
      const w = frame.current?.contentWindow
      if (!w) return
      w.addEventListener('keydown', (e: KeyboardEvent) => {
        const t = e.target as HTMLElement | null
        const typing = !!t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA')
        if (e.key === 'Escape' || (!typing && (e.key === 'p' || e.key === 'P'))) { e.preventDefault(); lower.current() }
      })
    } catch { /* cross-origin: should not happen, the page is ours */ }
  }

  useEffect(() => {
    const f = frame.current
    if (!f) return
    if (up) setTimeout(() => f.focus(), 350)
    else f.blur()
  }, [up])

  return (
    <div className={`f3-phone ${up ? 'up' : ''} ${buzzing ? 'buzz' : ''}`} aria-hidden={!up} data-testid="f3-phone">
      <div className="f3-phone-body">
        <div className="f3-phone-notch" />
        <iframe ref={frame} title="主管手機" src={`/phone${location.search}`} onLoad={hook} tabIndex={up ? 0 : -1} />
        <button className="f3-phone-down" onClick={onLower}>放下手機（P / Esc）</button>
      </div>
      <div className="f3-thumb" aria-hidden />
    </div>
  )
}
