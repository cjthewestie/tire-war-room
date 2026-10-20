// Phones and tablets (#26): a stick bottom-left walks (like WASD; pushed to the edge it runs), drag the view to look
// or orbit, pinch to zoom the overview, and three buttons stand in for F (what is in reach), P (phone) and V (view).
// The buttons replay the same key presses, so the keyboard handlers stay the one place the actions live.
import { useRef, useState, type PointerEvent } from 'react'
import { stick } from './pose'

const R = 48   // how far the knob travels, px

function press(code: string) {
  window.dispatchEvent(new KeyboardEvent('keydown', { code }))
  window.dispatchEvent(new KeyboardEvent('keyup', { code }))
}

export function TouchControls({ action, mode }: { action: string | null; mode: 'first' | 'third' }) {
  const base = useRef<HTMLDivElement>(null)
  const [knob, setKnob] = useState<{ x: number; y: number } | null>(null)
  const id = useRef<number | null>(null)

  const move = (e: PointerEvent) => {
    if (e.pointerId !== id.current || !base.current) return
    const r = base.current.getBoundingClientRect()
    let dx = e.clientX - (r.left + r.width / 2)
    let dy = e.clientY - (r.top + r.height / 2)
    const len = Math.hypot(dx, dy)
    if (len > R) { dx *= R / len; dy *= R / len }
    setKnob({ x: dx, y: dy })
    stick.st = dx / R
    stick.fw = -dy / R
  }
  const end = (e: PointerEvent) => {
    if (e.pointerId !== id.current) return
    id.current = null
    setKnob(null)
    stick.fw = 0; stick.st = 0
  }

  return (
    <>
      <div
        ref={base} className="f3-stick" data-testid="f3-stick" aria-label="移動搖桿"
        onPointerDown={(e) => { id.current = e.pointerId; e.currentTarget.setPointerCapture(e.pointerId); move(e) }}
        onPointerMove={move} onPointerUp={end} onPointerCancel={end}
      >
        <i style={knob ? { transform: `translate(${knob.x}px, ${knob.y}px)` } : undefined} />
      </div>
      <div className="f3-touch-btns">
        {action && <button className="f3-tbtn act" data-testid="f3-touch-act" onClick={() => press('KeyF')}>{action}</button>}
        <button className="f3-tbtn" onClick={() => press('KeyP')}>手機</button>
        <button className="f3-tbtn" onClick={() => press('KeyV')}>{mode === 'first' ? '俯瞰' : '第一人稱'}</button>
      </div>
    </>
  )
}
