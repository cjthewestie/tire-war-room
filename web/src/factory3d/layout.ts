// Floor plan of the 3D factory hall. Pure data: positions, footprints, colliders, colours.
// Machines come from ../factory (mirrored from warroom/factory.py), so the 28 ids stay in sync.
import type { Line, MachineStatus } from '../contracts'
import { MACHINES, type MachineMeta } from '../factory'

export type MachineKind =
  | 'mixer' | 'mill' | 'bigmill' | 'calender' | 'cooler' | 'winder'
  | 'hydraulic' | 'payoff' | 'extruder' | 'bladder' | 'cutter' | 'press'

const KIND: Record<string, MachineKind> = {
  'A-MIX': 'mixer', 'A-OPEN': 'mill', 'A-BIGOPEN': 'bigmill', 'A-SMALLOPEN': 'mill', 'A-CAL': 'calender',
  'A-COOL': 'cooler', 'A-WIND': 'winder', 'B-MIX': 'mixer', 'B-OPEN': 'mill', 'B-FORM': 'hydraulic',
  'B-ASSY': 'hydraulic', 'C-PAYOFF': 'payoff', 'C-EXTR': 'extruder', 'C-WIND': 'winder', 'C-WRAP': 'bladder',
  'D-CUT': 'cutter', 'BOND-01': 'hydraulic',
}

/** Footprint w (along x) × d (along z) and height, metres. */
export const KIND_SIZE: Record<MachineKind, [number, number, number]> = {
  mixer: [2.4, 2.4, 3.0], mill: [3.0, 1.8, 1.6], bigmill: [4.0, 2.2, 1.9], calender: [3.0, 2.0, 2.6],
  cooler: [4.0, 1.6, 1.2], winder: [2.0, 2.0, 1.8], hydraulic: [2.2, 2.2, 3.0], payoff: [1.6, 1.6, 1.6],
  extruder: [4.0, 1.4, 1.6], bladder: [2.0, 2.0, 1.8], cutter: [3.0, 1.6, 1.3], press: [2.4, 2.4, 2.4],
}

/** Row centre (z) per line. D and BOND share a row. */
const ROW_Z: Record<Line, number> = { A: -15, B: -8, C: -1, D: 6, BOND: 6, CURE: 14 }
const ROW_X0 = -22
const GAP: Record<Line, number> = { A: 2.0, B: 3.2, C: 3.2, D: 3.2, BOND: 3.2, CURE: 1.0 }

export interface Placed extends MachineMeta { kind: MachineKind; x: number; z: number; w: number; d: number; h: number }

function place(): Placed[] {
  const out: Placed[] = []
  const cursor: Record<string, number> = {}
  for (const m of MACHINES) {
    const kind: MachineKind = m.line === 'CURE' ? 'press' : KIND[m.id] ?? 'hydraulic'
    const [w0, d, h] = KIND_SIZE[kind]
    const w = m.id === 'A-SMALLOPEN' ? 2.4 : w0
    const row = m.line === 'BOND' ? 'D' : m.line
    const x0 = cursor[row] ?? ROW_X0
    out.push({ ...m, kind, x: x0 + w / 2, z: ROW_Z[m.line], w, d, h })
    cursor[row] = x0 + w + GAP[m.line]
  }
  return out
}

export const PLACED: Placed[] = place()
export const PLACED_BY_ID: Record<string, Placed> = Object.fromEntries(PLACED.map((p) => [p.id, p]))

/** Hall interior bounds (walls sit just outside). */
export const HALL = { x0: -27, x1: 31, z0: -20, z1: 20, h: 9 }

/** War room: raised plate in the east end, desk facing the big screen on the east wall. */
export const WAR_ROOM = { x0: 18, x1: 31, z0: -7, z1: 7 }
export const DESK = { x: 24.2, z: 0, w: 1.2, d: 4.0, h: 0.8 }
export const SCREEN = { x: 30.6, z: 0, w: 7.2, h: 4.0, y: 3.4 }

/** Player start: seated side of the desk, facing the big screen (yaw −π/2 = facing +x). Turn around for the hall. */
export const START = { x: 22.5, z: 0, yaw: -Math.PI / 2 }

export interface Box { x0: number; x1: number; z0: number; z1: number }
const PAD = 0.15
export const COLLIDERS: Box[] = [
  ...PLACED.map((p) => ({ x0: p.x - p.w / 2 - PAD, x1: p.x + p.w / 2 + PAD, z0: p.z - p.d / 2 - PAD, z1: p.z + p.d / 2 + PAD })),
  { x0: DESK.x - DESK.w / 2, x1: DESK.x + DESK.w / 2, z0: DESK.z - DESK.d / 2, z1: DESK.z + DESK.d / 2 },
]

export const PLAYER_R = 0.35

/** Push a circle (x, z, r) out of every box and clamp it inside the hall. */
export function collide(x: number, z: number, r = PLAYER_R): [number, number] {
  let px = Math.min(HALL.x1 - r, Math.max(HALL.x0 + r, x))
  let pz = Math.min(HALL.z1 - r, Math.max(HALL.z0 + r, z))
  for (const b of COLLIDERS) {
    const cx = Math.min(b.x1, Math.max(b.x0, px))
    const cz = Math.min(b.z1, Math.max(b.z0, pz))
    const dx = px - cx
    const dz = pz - cz
    const d2 = dx * dx + dz * dz
    if (d2 >= r * r) continue
    if (d2 > 1e-9) {
      const d = Math.sqrt(d2)
      px = cx + (dx / d) * r
      pz = cz + (dz / d) * r
    } else {
      // centre inside the box: leave by the nearest side
      const opts: [number, number, number][] = [
        [b.x0 - r - px, b.x0 - r, pz], [px - (b.x1 + r), b.x1 + r, pz],
        [b.z0 - r - pz, px, b.z0 - r], [pz - (b.z1 + r), px, b.z1 + r],
      ].map(([d, nx, nz]) => [Math.abs(d), nx, nz])
      opts.sort((a, c) => a[0] - c[0])
      px = opts[0][1]
      pz = opts[0][2]
    }
  }
  return [px, pz]
}

/** Distance from a point to a placed machine's footprint (0 when inside). */
export function distToMachine(p: Placed, x: number, z: number): number {
  const dx = Math.max(Math.abs(x - p.x) - p.w / 2, 0)
  const dz = Math.max(Math.abs(z - p.z) - p.d / 2, 0)
  return Math.hypot(dx, dz)
}

export type Light = 'green' | 'amber' | 'red' | 'grey'
export function statusLight(s: MachineStatus | undefined): Light {
  if (s === 'alarm' || s === 'stopped' || s === 'out_of_service') return 'red'
  if (s === 'warning') return 'amber'
  if (s === 'offline' || s === undefined) return 'grey'
  return 'green'
}
export const LIGHT_HEX: Record<Light, string> = { green: '#46d160', amber: '#f2b33d', red: '#ff3b30', grey: '#6b7178' }

export const LINE_HEX: Record<Line, string> = {
  A: '#8bc34a', B: '#64b5f6', C: '#b39ddb', D: '#ff8a50', BOND: '#ffd54f', CURE: '#ef5350',
}

export type Near = { kind: 'machine'; id: string } | { kind: 'desk' } | null

/** Nearest thing within reach: the desk, else the closest machine footprint within 2.2 m. */
export function nearest(x: number, z: number): Near {
  if (Math.hypot(x - DESK.x, z - DESK.z) < 2.8) return { kind: 'desk' }
  let best: { id: string; d: number } | null = null
  for (const p of PLACED) {
    const d = distToMachine(p, x, z)
    if (d < 2.2 && (!best || d < best.d)) best = { id: p.id, d }
  }
  return best ? { kind: 'machine', id: best.id } : null
}
