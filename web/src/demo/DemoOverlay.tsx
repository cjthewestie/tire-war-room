// The war room in demo mode: the 「🎬 Demo 模式」 bar with the cue's caption and the presenter controls, the spotlight
// on the beat's element (everything else dimmed, a soft glow, the short caption beside it) and, during the pitch, a calm
// full-screen card so the audience looks at the 3D whiteboard. Clicks pass through the dimmed area (CJ still presses
// 核准 here); the page itself is switched by App at each beat change.
import { useEffect, useLayoutEffect, useState } from 'react'
import { mss } from './cues'
import type { UseDemo } from './useDemo'
import './demo.css'

interface Rect { x: number; y: number; w: number; h: number }

/** The demo bar's height (one row on a big screen, two on a phone). */
const barH = () => (document.querySelector<HTMLElement>('.dm-bar')?.offsetHeight ?? 56) + 8

/** The first anchor on screen: one selector, or `a & b` (every part must be on screen, each gets a hole). */
function findAnchor(sel: string[]): HTMLElement[] {
  for (const s of sel) {
    const els = s.split(' & ').map((part) => {
      try { return document.querySelector<HTMLElement>(part) } catch { return null }   // a bad selector: next
    })
    if (els.every((el) => el && el.getBoundingClientRect().height > 0)) return els as HTMLElement[]
  }
  return []
}

/** An element's box, padded and clipped to the visible page under the demo bar (a long Agent trace stays inside). */
function clip(el: HTMLElement, pad = 8): Rect | null {
  const r = el.getBoundingClientRect()
  const top = Math.max(r.top - pad, barH() + 6), bottom = Math.min(r.bottom + pad, window.innerHeight - 6)
  const left = Math.max(r.left - pad, 6), right = Math.min(r.right + pad, window.innerWidth - 6)
  return bottom - top < 24 || right - left < 24 ? null : { x: left, y: top, w: right - left, h: bottom - top }
}

const WAIT_TEXT = { approval: '等主管核准（手機或核准卡）', event: '等工廠事件發生…', continue: '按 Space／N（或手把 A）繼續' } as const

export function DemoOverlay({ d, anchors, caption }: { d: UseDemo; anchors: string[]; caption: string }) {
  const { demo, cue, beat } = d
  const [rects, setRects] = useState<Rect[]>([])
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
    if (!anchors.length) { setRects([]); setHit(null); return }
    let scrolled: HTMLElement | null = null
    let settleAt = 0
    const tick = () => {
      const els = findAnchor(anchors)
      if (!els.length) { setRects([]); setHit(null); return }
      const el = els[0]
      // several parts (the Agent beat's H1 → 不成立 → H2): frame them together when they fit under the bar, and keep
      // them framed (the beat's compact layout lands a moment after the first scroll)
      const boxes = els.map((e) => e.getBoundingClientRect())
      const top = Math.min(...boxes.map((b) => b.top)), bottom = Math.max(...boxes.map((b) => b.bottom))
      const avail = window.innerHeight - barH()
      const fits = els.length > 1 && bottom - top <= avail - 24
      if (el !== scrolled) {
        // a tall element starts at the top (under the bar), a short one is centred
        scrolled = el; settleAt = performance.now() + 900
        const tall = el.getBoundingClientRect().height > avail - 40
        if (fits) window.scrollBy({ top: top - barH() - (avail - (bottom - top)) / 2, behavior: 'smooth' })
        else if (tall) window.scrollBy({ top: el.getBoundingClientRect().top - barH() - 12, behavior: 'smooth' })
        else el.scrollIntoView({ block: 'center', behavior: 'smooth' })
      } else if (fits && performance.now() > settleAt && (top < barH() || bottom > window.innerHeight)) {
        settleAt = performance.now() + 900
        window.scrollBy({ top: top - barH() - (avail - (bottom - top)) / 2, behavior: 'smooth' })
      }
      setHit(els.map((e) => e.dataset.testid ?? e.dataset.demoAnchor ?? e.className).join(' & '))
      const next = els.map((e) => clip(e)).filter((r): r is Rect => !!r)
      setRects((p) => (JSON.stringify(p) === JSON.stringify(next) ? p : next))
    }
    tick()
    const id = setInterval(tick, 200)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  // test hook (headless e2e only)
  useEffect(() => {
    const w = window as unknown as Record<string, unknown>
    w.__demoWarroom = () => ({ cue: demo?.cue_id, caption, anchors, hit, rects, paused: demo?.paused, waiting: demo?.waiting_for, pitch: !!beat?.pitch })
    return () => { delete w.__demoWarroom }
  }, [demo?.cue_id, demo?.paused, demo?.waiting_for, caption, anchors, hit, rects, beat?.pitch])

  if (!demo?.running) return null
  // the caption sits by the whole lit area (below it, else above it), never over one of its parts
  const r = rects.length ? rects.reduce((u, h) => {
    const x = Math.min(u.x, h.x), y = Math.min(u.y, h.y)
    return { x, y, w: Math.max(u.x + u.w, h.x + h.w) - x, h: Math.max(u.y + u.h, h.y + h.h) - y }
  }) : null
  const below = r ? r.y + r.h + 64 < window.innerHeight || r.y - 52 < barH() : true
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
          <button onClick={() => void d.cont()} disabled={demo.waiting_for === 'approval' || demo.waiting_for === 'event'} title="Space／N" data-testid="demo-continue">繼續 ▶</button>
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
          {/* the dim layer with a hole per anchor (an SVG mask, so two holes do not dim each other), then the glow */}
          <svg className="dm-dim" aria-hidden width="100%" height="100%">
            <defs>
              <mask id="dm-holes">
                <rect width="100%" height="100%" fill="white" />
                {rects.map((h, i) => <rect key={i} x={h.x} y={h.y} width={h.w} height={h.h} rx="14" fill="black" />)}
              </mask>
            </defs>
            <rect width="100%" height="100%" mask="url(#dm-holes)" />
          </svg>
          {rects.map((h, i) => <div key={i} className="dm-spot" style={{ left: h.x, top: h.y, width: h.w, height: h.h }} data-testid="demo-spot" aria-hidden />)}
          {caption && (
            <div className={`dm-cap ${below ? 'below' : 'above'}`} data-testid="demo-caption"
              style={{ left: Math.max(12, Math.min(r.x, window.innerWidth - 420)), top: below ? Math.min(r.y + r.h + 12, window.innerHeight - 64) : r.y - 12 }}>{caption}</div>
          )}
        </>
      ) : caption ? (
        <div className="dm-cap only" data-testid="demo-caption">{caption}</div>
      ) : null}
    </>
  )
}
