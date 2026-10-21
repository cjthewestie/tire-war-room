// Xbox controller for the 3D factory (CJ, 2026-10-22: 「打開摩尼工廠我要用搖桿操作」). Browser Gamepad API, "standard"
// layout (Xbox first; PS / Switch Pro map the same buttons). Pure parts here (dead zone, smoothing, button edges, holds),
// so e2e/gamepad-unit.mjs tests them with a fake Gamepad; usePad() polls navigator.getGamepads() every animation frame
// and hands FactoryApp the actions. macOS Chrome / Safari only report a pad after a button press on the page, so the
// HUD says 「按任一鍵連接搖桿」 until then.
import { useEffect, useRef, useState } from 'react'

/** Standard-mapping button indices, with the Xbox names the help overlay shows. */
export const BTN = { A: 0, B: 1, X: 2, Y: 3, LB: 4, RB: 5, LT: 6, RT: 7, View: 8, Menu: 9, LS: 10, RS: 11, Up: 12, Down: 13, Left: 14, Right: 15 } as const
export type Btn = keyof typeof BTN
export const DEAD = 0.15
/** Holding B this long asks 「離開 Demo？」; holding View shows the help. */
export const HOLD_EXIT_S = 1

/** Dead zone with rescale: |v| < DEAD → 0, then 0…1 again (so a gentle push is a gentle move, no jump at the edge). */
export function dead(v: number, dz = DEAD): number {
  const a = Math.abs(v)
  if (!Number.isFinite(v) || a < dz) return 0
  return Math.sign(v) * Math.min(1, (a - dz) / (1 - dz))
}
/** Radial dead zone for a stick (x, y): diagonal pushes are not cut short. */
export function deadStick(x: number, y: number, dz = DEAD): [number, number] {
  const m = Math.hypot(x, y)
  if (!Number.isFinite(m) || m < dz) return [0, 0]
  const k = Math.min(1, (m - dz) / (1 - dz)) / m
  return [x * k, y * k]
}

export interface PadFrame { lx: number; ly: number; rx: number; ry: number; lt: number; rt: number; down: boolean[] }
type GamepadLike = { axes: readonly number[]; buttons: readonly { pressed: boolean; value: number }[]; mapping?: string }

/** One reading: sticks after the dead zone, triggers 0…1, which buttons are down. */
export function readPad(gp: GamepadLike): PadFrame {
  const ax = (i: number) => gp.axes[i] ?? 0
  const [lx, ly] = deadStick(ax(0), ax(1))
  const [rx, ry] = deadStick(ax(2), ax(3))
  const b = (i: number) => gp.buttons[i]
  const trig = (i: number) => dead(b(i)?.value ?? (b(i)?.pressed ? 1 : 0), 0.08)
  return { lx, ly, rx, ry, lt: trig(BTN.LT), rt: trig(BTN.RT), down: Object.values(BTN).map((i) => !!b(i)?.pressed || (b(i)?.value ?? 0) > 0.5) }
}

/** Buttons that went down this frame (edge, so holding A is one press). */
export function pressed(prev: boolean[] | null, cur: boolean[]): Btn[] {
  const names = Object.keys(BTN) as Btn[]
  return names.filter((n) => cur[BTN[n]] && !(prev?.[BTN[n]] ?? false))
}
export function released(prev: boolean[] | null, cur: boolean[]): Btn[] {
  const names = Object.keys(BTN) as Btn[]
  return names.filter((n) => !cur[BTN[n]] && !!prev?.[BTN[n]])
}

/** Smooth a stick toward its target: speeds ease in and out, capped (no motion sickness). */
export function approach(cur: number, target: number, dt: number, rate = 6): number {
  return cur + (target - cur) * Math.min(1, dt * rate)
}

/** How long each button has been held (s); reset on release. */
export function holds(prev: Record<number, number>, down: boolean[], dt: number): Record<number, number> {
  const out: Record<number, number> = {}
  down.forEach((d, i) => { if (d) out[i] = (prev[i] ?? 0) + dt })
  return out
}

export interface PadState { connected: boolean; id: string | null; frame: PadFrame | null; help: boolean }

/** Poll the first standard pad every animation frame. `onFrame` gets the smoothed sticks and the edges. */
export function usePad(onFrame: (f: PadFrame, edges: Btn[], held: Record<number, number>, dt: number) => void): PadState {
  const [st, setSt] = useState<PadState>({ connected: false, id: null, frame: null, help: false })
  const cb = useRef(onFrame)
  useEffect(() => { cb.current = onFrame }, [onFrame])
  useEffect(() => {
    if (typeof navigator === 'undefined' || !navigator.getGamepads) return
    let raf = 0, last = performance.now(), prev: boolean[] | null = null, held: Record<number, number> = {}
    let conn = false, help = false
    const onConn = () => { /* the poll picks it up; the event is only a hint */ }
    window.addEventListener('gamepadconnected', onConn)
    const loop = () => {
      raf = requestAnimationFrame(loop)
      const now = performance.now(), dt = Math.min(0.1, (now - last) / 1000)
      last = now
      const gp = [...navigator.getGamepads()].find((g) => g && g.connected && (g.mapping === 'standard' || g.buttons.length >= 16)) ?? null
      if (!gp) {
        if (conn) { conn = false; prev = null; held = {}; setSt({ connected: false, id: null, frame: null, help: false }) }
        return
      }
      const f = readPad(gp)
      if (!conn) { conn = true; setSt({ connected: true, id: gp.id, frame: f, help: false }) }
      const edges = pressed(prev, f.down)
      held = holds(held, f.down, dt)
      prev = f.down
      const h = (held[BTN.View] ?? 0) > 0.25
      if (h !== help) { help = h; setSt((s) => ({ ...s, help: h })) }
      cb.current(f, edges, held, dt)
    }
    raf = requestAnimationFrame(loop)
    return () => { cancelAnimationFrame(raf); window.removeEventListener('gamepadconnected', onConn) }
  }, [])
  return st
}

/** The help overlay's rows: [button, free mode, demo mode]. */
export const PAD_HELP: [string, string, string][] = [
  ['左搖桿', '走路（推到底是跑）', '—（視角鎖定）'],
  ['右搖桿', '看四周／俯瞰時繞圈', '—'],
  ['LT／RT', '俯瞰：拉遠／拉近', '—'],
  ['十字鍵', '換機台（← → 同排、↑ ↓ 換排）', '—'],
  ['A', '打開機台讀數／操作', '繼續（下一步）'],
  ['B', '返回／關閉', '按住 1 秒：離開 Demo'],
  ['X', '拿起手機', '拿起手機（核准）'],
  ['Y', '飛到異常機台（沒有就飛到白板）', '—'],
  ['LB／RB', '白板前：上一頁／下一頁', 'Pitch：上一頁／下一頁'],
  ['Menu', '開始 Demo（6 分鐘）', '暫停／繼續'],
  ['View（按住）', '顯示這張說明', '顯示這張說明'],
]
