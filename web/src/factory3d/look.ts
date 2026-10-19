// Pointer lock for first-person mouse look. The element is the canvas wrapper (the canvas itself lets pointer events
// through, for the big screen's blending occlusion).
import { view } from './pose'

export const lock = { el: null as HTMLElement | null }

/** Ask for pointer lock (mouse look). Needs a user gesture; refused quietly (headless, or the user said no). */
export function requestLook() {
  const el = lock.el
  if (view.mode !== 'first' || !el || document.pointerLockElement === el) return
  try {
    const r = el.requestPointerLock?.() as unknown
    if (r && typeof (r as Promise<void>).catch === 'function') (r as Promise<void>).catch(() => {})
  } catch { /* not allowed */ }
}
export function releaseLook() {
  try { if (document.pointerLockElement) document.exitPointerLock() } catch { /* ignore */ }
}
