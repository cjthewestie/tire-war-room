// Which input does what, with the demo's camera lock (CJ, 2026-10-22: 「demo按鍵按下去就只能按照流程跑」): pure, so
// e2e/gamepad-unit.mjs can check that free inputs are ignored during the demo and work again after it. FactoryApp
// runs the actions. Keyboard and the Xbox pad map to the same actions:
//   continue  Space / N        A          pause  B        Menu       exit  Esc (confirm)   hold B 1 s (confirm)
//   slides    ← → PgUp PgDn    LB RB      phone  P        X          start G (not in a demo)  Menu
import { BTN, HOLD_EXIT_S, type Btn, type PadFrame } from './gamepad'

export type Action =
  | 'start' | 'continue' | 'pause' | 'exit-ask' | 'exit-yes' | 'exit-no' | 'slide+' | 'slide-' | 'slides-close' | 'phone'
  // free mode only
  | 'open' | 'back' | 'focus+' | 'focus-' | 'row+' | 'row-' | 'fly'

export interface Ctx {
  demoOn: boolean
  confirmExit: boolean
  /** the pitch board is in view (demo pitch, the full-screen slides, or standing at the board) */
  slides: boolean
  slideView: boolean
  phoneUp: boolean
}

/** A key in demo terms; null = not a demo key (free keys are handled by the 3D's own handler, and only when !demoOn). */
export function demoKey(code: string, c: Ctx): Action | null {
  if (!c.demoOn) return code === 'KeyG' ? 'start' : null
  if (c.confirmExit) return code === 'Enter' ? 'exit-yes' : code === 'Escape' ? 'exit-no' : null
  if (code === 'Space' || code === 'KeyN') return 'continue'
  if (code === 'KeyB') return 'pause'
  if (code === 'KeyP') return 'phone'
  if (c.slides && (code === 'ArrowRight' || code === 'PageDown')) return 'slide+'
  if (code === 'ArrowRight') return 'continue'   // auto-play's 下一階段 (outside the slides)
  if (c.slides && (code === 'ArrowLeft' || code === 'PageUp')) return 'slide-'
  if (code === 'Escape') return c.slideView ? 'slides-close' : c.phoneUp ? 'phone' : 'exit-ask'
  return null
}

/** Is a free camera / movement input (mouse, wheel, WASD, sticks, D-pad) accepted now? */
export const freeInput = (c: Pick<Ctx, 'demoOn'>) => !c.demoOn

export interface PadOut { actions: Action[]; move: [number, number]; look: [number, number]; zoom: number }

/** One pad frame: the actions it triggers and the free movement it asks for (all zero while the demo runs). */
export function padActions(f: PadFrame, edges: Btn[], held: Record<number, number>, c: Ctx, exitAsked: boolean): PadOut {
  const on = (b: Btn) => edges.includes(b)
  const a: Action[] = []
  if (c.demoOn) {
    if (c.confirmExit) {
      if (on('A')) a.push('exit-yes')
      else if (on('B')) a.push('exit-no')
      return { actions: a, move: [0, 0], look: [0, 0], zoom: 0 }
    }
    if ((held[BTN.B] ?? 0) >= HOLD_EXIT_S && !exitAsked) a.push('exit-ask')
    if (on('A')) a.push('continue')
    if (on('Menu')) a.push('pause')
    if (on('X')) a.push('phone')
    if (c.slides && on('RB')) a.push('slide+')
    if (c.slides && on('LB')) a.push('slide-')
    if (c.slideView && on('B')) a.push('slides-close')
    return { actions: a, move: [0, 0], look: [0, 0], zoom: 0 }
  }
  if (on('Menu')) a.push('start')
  if (on('A')) a.push('open')
  if (on('B')) a.push(c.slideView ? 'slides-close' : 'back')
  if (on('X')) a.push('phone')
  if (on('Y')) a.push('fly')
  if (on('Right')) a.push('focus+')
  if (on('Left')) a.push('focus-')
  if (on('Down')) a.push('row+')
  if (on('Up')) a.push('row-')
  if (c.slides && on('RB')) a.push('slide+')
  if (c.slides && on('LB')) a.push('slide-')
  return { actions: a, move: [-f.ly, f.lx], look: [f.rx, f.ry], zoom: f.lt - f.rt }
}
