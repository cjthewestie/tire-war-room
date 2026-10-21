// The war room in demo mode: the 「🎬 Demo 模式」 bar with the cue's caption and the presenter controls, the spotlight
// on the beat's element (everything else dimmed, a soft glow, the short caption beside it) and, during the pitch, a calm
// full-screen card so the audience looks at the 3D whiteboard. Clicks pass through the dimmed area (CJ still presses
// 核准 here); the page itself is switched by App at each beat change.
import { useEffect, useLayoutEffect, useState } from 'react'
import { mss } from './cues'
import type { UseDemo } from './useDemo'
import './demo.css'

interface Rect { x: number; y: number; w: number; h: number }

/** The first selector whose element is on screen. */
function findAnchor(sel: string[]): HTMLElement | null {
  for (const s of sel) {
    let el: HTMLElement | null = null
    try { el = document.querySelector<HTMLElement>(s) } catch { /* a bad selector: next */ }
    if (el && el.getBoundingClientRect().height > 0) return el
  }
  return null
}

const WAIT_TEXT = { approval: '等主管核准（手機或核准卡）', event: '等工廠事件發生…', continue: '按 Space／N（或手把 A）繼續' } as const

export function DemoOverlay({ d, anchors, caption }: { d: UseDemo; anchors: string[]; caption: string }) {
  const { demo, cue, beat } = d
  const [rect, setRect] = useState<Rect | null>(null)
  const [hit, setHit] = useState<string | null>(null)
  const [t, setT] = useState(demo?.elapsed_s ?? 0)
  const key = anchors.join('|')

  // the demo clock shown in the bar (server elapsed at the last push, counted on locally while running)
  useEffect(() => {
    if (!demo?.running) return
    const base = demo.elapsed_s, at = performance.now()
    setT(base)
    if (demo.paused) return
    const id = setInterval(() => setT(base + (performance.now() - at) / 1000), 500)
    return () => clearInterval(id)
  }, [demo?.running, demo?.elapsed_s, demo?.paused, demo?.rev])

  // the spotlight follows its element (late renders, scrolling, resizing); scrolled into view once per anchor
  useLayoutEffect(() => {
    if (!anchors.length) { setRect(null); setHit(null); return }
    let scrolled: HTMLElement | null = null
    const tick = () => {
      const el = findAnchor(anchors)
      if (!el) { setRect(null); setHit(null); return }
      if (el !== scrolled) { scrolled = el; el.scrollIntoView({ block: 'center', behavior: 'smooth' }) }
      const r = el.getBoundingClientRect()
      setHit(el.dataset.testid ?? el.dataset.demoAnchor ?? el.className)
      setRect((p) => (p && p.x === r.x && p.y === r.y && p.w === r.width && p.h === r.height ? p : { x: r.x, y: r.y, w: r.width, h: r.height }))
    }
    tick()
    const id = setInterval(tick, 200)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  // test hook (headless e2e only)
  useEffect(() => {
    const w = window as unknown as Record<string, unknown>
    w.__demoWarroom = () => ({ cue: demo?.cue_id, caption, anchors, hit, rect, paused: demo?.paused, waiting: demo?.waiting_for, pitch: !!beat?.pitch })
    return () => { delete w.__demoWarroom }
  }, [demo?.cue_id, demo?.paused, demo?.waiting_for, caption, anchors, hit, rect, beat?.pitch])

  if (!demo?.running) return null
  const pad = 8
  const r = rect && { x: rect.x - pad, y: rect.y - pad, w: rect.w + pad * 2, h: rect.h + pad * 2 }
  const below = r ? r.y + r.h + 64 < window.innerHeight : true
  const wait = demo.waiting_for ? WAIT_TEXT[demo.waiting_for] : null
  return (
    <>
      <div className="dm-bar" role="region" aria-label="Demo 模式" data-testid="demo-bar">
        <span className="dm-badge">🎬 Demo 模式</span>
        <span className="dm-time" title="Demo 時間">{mss(t)}</span>
        <div className="dm-cue">
          <b data-testid="demo-cue-title">{cue?.title ?? demo.cue_id}</b>
          {cue?.body && <span className="dm-body">{cue.body}</span>}
        </div>
        {wait && <span className={`dm-wait w-${demo.waiting_for}`} data-testid="demo-wait">{wait}</span>}
        {demo.paused && <span className="dm-wait w-paused">已暫停</span>}
        {d.err && <span className="dm-err" role="alert" data-testid="demo-err">{d.err}</span>}
        <div className="dm-ctl">
          <button onClick={() => void d.cont()} disabled={demo.waiting_for === 'approval'} title="Space／N" data-testid="demo-continue">繼續 ▶</button>
          <button onClick={() => void d.togglePause()} title="P" data-testid="demo-pause">{demo.paused ? '▶ 恢復' : '❚❚ 暫停'}</button>
          <button onClick={() => void d.exit()} title="Esc" className="dm-exit" data-testid="demo-exit">✕ 結束</button>
        </div>
      </div>
      {beat?.pitch ? (
        <div className="dm-pitch" data-testid="demo-pitch">
          <div className="dm-pitch-in">
            <span className="dm-pitch-k">{caption}</span>
            <b>實心胎智慧製造戰情室</b>
            <span className="dm-pitch-s">請看 3D 工廠的白板</span>
          </div>
        </div>
      ) : r ? (
        <>
          <div className="dm-spot" style={{ left: r.x, top: r.y, width: r.w, height: r.h }} data-testid="demo-spot" aria-hidden />
          {caption && (
            <div className={`dm-cap ${below ? 'below' : 'above'}`} data-testid="demo-caption"
              style={{ left: Math.max(12, Math.min(r.x, window.innerWidth - 420)), top: below ? r.y + r.h + 12 : r.y - 12 }}>{caption}</div>
          )}
        </>
      ) : caption ? (
        <div className="dm-cap only" data-testid="demo-caption">{caption}</div>
      ) : null}
    </>
  )
}
