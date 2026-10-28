// Sitting down at the war-room computer (F at the desk): the REAL dashboard page (/) full-size, framed as the desk
// monitor, fully usable (Agent 對話台 first, 產線監控 / 排程與人力 tabs, the 展示 strip, approvals). Same origin, so it talks to the same
// backend through its own code. Mounted only while open, so the page holds one extra /ws connection only then.
// Esc or F (outside a text field) inside the page stands up again, and so does 「離開電腦 ✕」. The key listener is
// attached on every load and reads `demo` when a key comes (CJ 10-22 「按F無法離開戰情室頁面」: after the auto demo
// ended at the desk the same iframe stayed up with demo → false, but its listener had been skipped at load).
import { useEffect, useRef } from 'react'
import { embedSearch } from '../transport'

/** `hash`: the page to open on (the guided demo: #agent 主控台, #console Agent 頁面); `demo`: the guided demo drives it
 *  (its keys stay with the demo, so the page does not take Esc / F). */
export function DeskMonitor({ onClose, hash = '', demo = false }: { onClose: () => void; hash?: string; demo?: boolean }) {
  const frame = useRef<HTMLIFrameElement>(null)
  const close = useRef(onClose)
  useEffect(() => { close.current = onClose }, [onClose])
  const demoRef = useRef(demo)
  useEffect(() => {
    demoRef.current = demo
    // the demo just handed the desk over: give the page the keys (F / Esc stand up)
    if (!demo) { try { frame.current?.focus() } catch { /* ignore */ } }
  }, [demo])

  const hook = () => {
    try {
      const w = frame.current?.contentWindow
      if (!w) return
      w.addEventListener('keydown', (e: KeyboardEvent) => {
        if (demoRef.current) return          // the guided demo drives the desk: its keys stay with the demo
        const t = e.target as HTMLElement | null
        const typing = !!t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)
        if (e.key === 'Escape' || (!typing && (e.key === 'f' || e.key === 'F'))) { e.preventDefault(); e.stopPropagation(); close.current() }
      }, true)
      if (!demoRef.current) frame.current?.focus()
    } catch { /* cross-origin: should not happen, the page is ours */ }
  }

  return (
    <div className="f3-monitor-wrap" data-testid="f3-monitor">
      <div className="f3-monitor" role="dialog" aria-label="戰情室電腦">
        <div className="f3-monitor-screen">
          <iframe ref={frame} title="戰情室儀表板" src={`/${embedSearch()}${hash}`} onLoad={hook} />
        </div>
        <div className="f3-monitor-chin">
          <span className="f3-monitor-led" />
          <b>戰情室電腦</b>
          {!demo && <button className="f3-monitor-up" data-testid="desk-leave" onClick={onClose}>離開電腦 ✕（Esc / F）</button>}
        </div>
      </div>
      <div className="f3-monitor-stand" aria-hidden />
    </div>
  )
}
