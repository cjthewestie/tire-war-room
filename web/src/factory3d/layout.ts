// Floor plan of the miniature factory island. Pure data: positions, footprints, colliders, colours.
// Units are "figure heights" (the chibi lead is ~1 unit tall), so the whole works is a ~21 × 15 diorama.
// Machines come from ../factory (mirrored from warroom/factory.py), so the 28 ids stay in sync.
import type { Line, MachineStatus } from '../contracts'
import { MACHINES, type MachineMeta } from '../factory'
import { squircle, wobble } from './kit'

export type MachineKind =
  | 'mixer' | 'mill' | 'bigmill' | 'calender' | 'cooler' | 'winder'
  | 'hydraulic' | 'payoff' | 'extruder' | 'bladder' | 'cutter' | 'press'

const KIND: Record<string, MachineKind> = {
  'A-MIX': 'mixer', 'A-OPEN': 'mill', 'A-BIGOPEN': 'bigmill', 'A-SMALLOPEN': 'mill', 'A-CAL': 'calender',
  'A-COOL': 'cooler', 'A-WIND': 'winder', 'B-MIX': 'mixer', 'B-OPEN': 'mill', 'B-FORM': 'hydraulic',
  'B-ASSY': 'hydraulic', 'C-PAYOFF': 'payoff', 'C-EXTR': 'extruder', 'C-WIND': 'winder', 'C-WRAP': 'bladder',
  'D-CUT': 'cutter', 'BOND-01': 'hydraulic',
}

/** Toy footprint w (along x) × d (along z) and body height. */
export const KIND_SIZE: Record<MachineKind, [number, number, number]> = {
  mixer: [0.9, 0.9, 1.0], mill: [1.0, 0.7, 0.6], bigmill: [1.3, 0.8, 0.7], calender: [1.0, 0.75, 0.95],
  cooler: [1.3, 0.6, 0.5], winder: [0.75, 0.75, 0.7], hydraulic: [0.85, 0.85, 1.0], payoff: [0.65, 0.65, 0.65],
  extruder: [1.3, 0.6, 0.65], bladder: [0.75, 0.75, 0.7], cutter: [1.0, 0.6, 0.6], press: [0.72, 0.78, 0.85],
}

/** Row centre (z) per line. D and BOND share a row; the curing presses are the long south row. */
export const ROW_Z: Record<Line, number> = { A: -4.6, B: -2.4, C: -0.2, D: 2.0, BOND: 2.0, CURE: 4.4 }
export const ROW_X0 = -6.6
const GAP: Record<Line, number> = { A: 0.45, B: 0.9, C: 0.9, D: 0.9, BOND: 0.9, CURE: 0.22 }

export interface Placed extends MachineMeta { kind: MachineKind; x: number; z: number; w: number; d: number; h: number }

function place(): Placed[] {
  const out: Placed[] = []
  const cursor: Record<string, number> = {}
  for (const m of MACHINES) {
    const kind: MachineKind = m.line === 'CURE' ? 'press' : KIND[m.id] ?? 'hydraulic'
    const [w0, d, h] = KIND_SIZE[kind]
    const w = m.id === 'A-SMALLOPEN' ? 0.85 : w0
    const row = m.line === 'BOND' ? 'D' : m.line
    const x0 = cursor[row] ?? ROW_X0
    out.push({ ...m, kind, x: x0 + w / 2, z: ROW_Z[m.line], w, d, h })
    cursor[row] = x0 + w + GAP[m.line]
  }
  return out
}

export const PLACED: Placed[] = place()
export const PLACED_BY_ID: Record<string, Placed> = Object.fromEntries(PLACED.map((p) => [p.id, p]))

/** The island: squircle half-extents (grass edge before wobble) and the wobble seed. */
export const ISLAND = { A: 10.6, B: 7.7, seed: 2.3, ground: '#9dbb6e' }
/** The walkable grass stops this far inside the grass edge. */
const EDGE_MARGIN = 0.45

/** Grass-edge radius of the island at angle a. */
export function islandRadius(a: number) {
  return squircle(a, ISLAND.A, ISLAND.B) * wobble(a, ISLAND.seed)
}

/** War-room hut on the east end: open toward the factory (west), big screen on the back (east) wall. */
export const WAR_ROOM = { x0: 4.9, x1: 8.1, z0: -1.7, z1: 1.7 }
export const DESK = { x: 6.95, z: 0, w: 0.45, d: 1.5, h: 0.42 }
export const SCREEN = { x: 7.98, z: 0, w: 2.4, h: 1.35, y: 1.2 }

/** Player start: in front of the desk, facing the big screen (yaw −π/2 = facing +x). */
export const START = { x: 6.2, z: 0, yaw: -Math.PI / 2 }

export interface Box { x0: number; x1: number; z0: number; z1: number }
const PAD = 0.04
export const COLLIDERS: Box[] = [
  ...PLACED.map((p) => ({ x0: p.x - p.w / 2 - PAD, x1: p.x + p.w / 2 + PAD, z0: p.z - p.d / 2 - PAD, z1: p.z + p.d / 2 + PAD })),
  { x0: DESK.x - DESK.w / 2, x1: DESK.x + DESK.w / 2, z0: DESK.z - DESK.d / 2, z1: DESK.z + DESK.d / 2 },
  { x0: WAR_ROOM.x1 - 0.12, x1: WAR_ROOM.x1 + 0.12, z0: WAR_ROOM.z0 - 0.1, z1: WAR_ROOM.z1 + 0.1 },   // back wall
  { x0: WAR_ROOM.x0 + 0.5, x1: WAR_ROOM.x1, z0: WAR_ROOM.z0 - 0.06, z1: WAR_ROOM.z0 + 0.06 },           // side walls
  { x0: WAR_ROOM.x0 + 0.5, x1: WAR_ROOM.x1, z0: WAR_ROOM.z1 - 0.06, z1: WAR_ROOM.z1 + 0.06 },
]

export const PLAYER_R = 0.22

/** Is (x, z) on the walkable grass? */
export function onIsland(x: number, z: number, r = PLAYER_R) {
  return Math.hypot(x, z) <= islandRadius(Math.atan2(z, x)) - EDGE_MARGIN - r
}

/** Push a circle (x, z, r) out of every box and keep it on the island (no walking into the sea). */
export function collide(x: number, z: number, r = PLAYER_R): [number, number] {
  let px = x
  let pz = z
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
  const a = Math.atan2(pz, px)
  const lim = islandRadius(a) - EDGE_MARGIN - r
  const rr = Math.hypot(px, pz)
  if (rr > lim) { px *= lim / rr; pz *= lim / rr }
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
export const LIGHT_HEX: Record<Light, string> = { green: '#3fcf5a', amber: '#f5a623', red: '#ff3b30', grey: '#8a8f96' }

/** Line colours: the dashboard's line hues, softened to pastels for the island. */
export const LINE_HEX: Record<Line, string> = {
  A: '#8fbf5a', B: '#6fa8dc', C: '#a993d6', D: '#f29a6b', BOND: '#e8c15a', CURE: '#e07a74',
}

export type Near = { kind: 'machine'; id: string } | { kind: 'desk' } | null

/** Nearest thing within reach: the desk, else the closest machine footprint within 0.75. */
export function nearest(x: number, z: number): Near {
  const ddx = Math.max(Math.abs(x - DESK.x) - DESK.w / 2, 0)
  const ddz = Math.max(Math.abs(z - DESK.z) - DESK.d / 2, 0)
  if (Math.hypot(ddx, ddz) < 0.8) return { kind: 'desk' }
  let best: { id: string; d: number } | null = null
  for (const p of PLACED) {
    const d = distToMachine(p, x, z)
    if (d < 0.75 && (!best || d < best.d)) best = { id: p.id, d }
  }
  return best ? { kind: 'machine', id: best.id } : null
}

/** Background workers' walking loops (aisles and the open yard; they ignore the player). */
export const WALKER_LOOPS: [number, number][][] = [
  [[-6.8, -3.5], [3.6, -3.5], [3.6, -1.3], [-6.8, -1.3]],
  [[0.2, 0.9], [4.2, 0.9], [4.2, 3.0], [0.2, 3.0]],
  [[-6.8, 3.25], [3.8, 3.25], [3.8, 5.6], [-6.8, 5.6]],
]
