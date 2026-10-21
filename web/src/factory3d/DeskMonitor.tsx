// Sitting down at the war-room computer (F at the desk): the REAL dashboard page (/) full-size, framed as the desk
// monitor, fully usable (Agent 對話台 first, 產線監控 / 排程與人力 tabs, the 展示 strip, approvals). Same origin, so it talks to the same
// backend through its own code. Mounted only while open, so the page holds one extra /ws connection only then.
// Esc or F (outside a text field) inside the page stands up again.
import { useEffect, useRef } from 'react'
import { embedSearch } from '../transport'

export function DeskMonitor({ onClose }: { onClose: () => void }) {
  const frame = useRef<HTMLIFrameElement>(null)
  const close = useRef(onClose)
  useEffect(() => { close.current = onClose }, [onClose])

  const hook = () => {
    try {
      const w = frame.current?.contentWindow
      if (!w) return
      w.addEventListener('keydown', (e: KeyboardEvent) => {
        const t = e.target as HTMLElement | null
        const typing = !!t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)
        if (e.key === 'Escape' || (!typing && (e.key === 'f' || e.key === 'F'))) { e.preventDefault(); close.current() }
      })
      frame.current?.focus()
    } catch { /* cross-origin: should not happen, the page is ours */ }
  }

  return (
    <div className="f3-monitor-wrap" data-testid="f3-monitor">
      <div className="f3-monitor" role="dialog" aria-label="戰情室電腦">
        <div className="f3-monitor-screen">
          <iframe ref={frame} title="戰情室儀表板" src={`/${embedSearch()}`} onLoad={hook} />
        </div>
        <div className="f3-monitor-chin">
          <span className="f3-monitor-led" />
          <b>戰情室電腦</b>
          <button className="f3-monitor-up" onClick={onClose}>起身（Esc / F）</button>
        </div>
      </div>
      <div className="f3-monitor-stand" aria-hidden />
    </div>
  )
}
