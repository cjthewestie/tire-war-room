// The war room in demo mode looks like everyday use (CJ, 2026-10-22): the shared cue switches the page (App), selects
// the beat's item and scrolls it into view here; the only visible demo element is a small control pill at the bottom
// (繼續 / 暫停 / ✕, the demo clock). No dimming, spotlight or caption card. Clicks work as usual (CJ presses 核准 here).
import { useEffect, useLayoutEffect, useState } from 'react'
import { mss } from './cues'
import type { UseDemo } from './useDemo'
import './demo.css'

interface Rect { x: number; y: number; w: number; h: number }

/** Space kept above a framed item when it is scrolled into view (the demo has no top bar). */
const barH = () => 8

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
        else if (els.length > 1) window.scrollBy({ top: el.getBoundingClientRect().top - 96, behavior: 'smooth' })   // the first part near the top
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
  // CJ, 2026-10-22: 「demo進戰情室看板的時候不需要那麼顯眼的提示跟一般一樣就好了讓評審看到使用的樣子」: the page looks like
  // everyday use (no dimming, no spotlight, no caption card); the cue still picks the page, selects the item and scrolls
  // to it. All that shows of the demo is this small control pill at the bottom.
  const hint = demo.waiting_for === 'approval' ? '等核准' : demo.paused ? '已暫停' : null
  return (
    <div className="dm-mini" role="region" aria-label="Demo 控制" data-testid="demo-bar" title={cue?.title ?? demo.cue_id ?? ''}>
      <span className="dm-mini-dot" aria-hidden>🎬</span>
      <span className="dm-time" title="Demo 時間">{mss(t)}</span>
      {hint && <span className="dm-mini-hint" data-testid="demo-wait">{hint}</span>}
      {d.err && <span className="dm-err" role="alert" data-testid="demo-err" title={d.err}>{d.err}</span>}
      <button onClick={() => void d.cont()} disabled={demo.waiting_for === 'approval' || demo.waiting_for === 'event'} title="繼續（Space／N）" aria-label="繼續" data-testid="demo-continue">繼續 ▶</button>
      <button onClick={() => void d.togglePause()} title="暫停／恢復（P）" aria-label={demo.paused ? '恢復' : '暫停'} data-testid="demo-pause">{demo.paused ? '▶' : '❚❚'}</button>
      <button onClick={() => void d.exit()} title="結束（Esc）" aria-label="結束 Demo" data-testid="demo-exit">✕</button>
    </div>
  )
}
