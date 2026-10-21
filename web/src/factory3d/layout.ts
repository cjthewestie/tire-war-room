// Floor plan of the miniature factory building. Pure data: positions, footprints, colliders, colours.
// Units are "figure heights" (the chibi lead is ~1 unit tall), so the whole building is a ~20 × 12 dollhouse.
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

/** The factory building (dollhouse cutaway): outer-wall lines. The production floor is the west part; the east strip
 *  (x > ROOM_X) holds an open lobby (north) with the door to the 休息區, the war room + staff corner (middle), and
 *  the loading area by the big doors (south) with the door to the 廁所 on the east wall. The 休息區 and 廁所 are
 *  separate spaces (spaces.ts): walking through their door teleports the lead there. */
export const BUILDING = { x0: -8.0, x1: 11.6, z0: -6.0, z1: 6.2 }
/** Partition between the production floor and the east rooms. */
export const ROOM_X = 4.8
/** Wall thickness (outer and inner). */
export const WALL_T = 0.16

export const ROOMS = {
  warRoom: { x0: ROOM_X, x1: 9.6, z0: -2.4, z1: 2.4, label: '戰情室' },
  staff: { x0: 9.6, x1: 11.6, z0: -2.4, z1: 2.4, label: '員工待命區' },
} as const
export const WAR_ROOM = ROOMS.warRoom
/** Big roll-up doors in the south wall, with the loading dock outside. */
export const DOOR = { x0: 5.6, x1: 8.6 }
export const DOCK = { x0: 5.2, x1: 9.0, z0: 6.2, z1: 8.0 }
/** Staff corner: where the four idle workers stand. */
/** [x, z, yaw]: yaw as for the lead (π = facing +z), so they look out over the corridor and the doors. */
export const STAFF_SPOTS: [number, number, number][] = [[10.1, -1.3, Math.PI + 0.5], [10.85, -0.6, Math.PI - 0.3], [10.15, 0.4, Math.PI + 0.2], [10.85, 1.2, Math.PI - 0.4]]

export const DESK = { x: 8.35, z: 0, w: 0.45, d: 1.5, h: 0.42 }
export const SCREEN = { x: 9.5, z: 0, w: 2.4, h: 1.35, y: 1.2 }

export type SceneId = 'factory' | 'break' | 'toilet'
/** Doors to the other spaces, painted on an outer wall. (x, z) is the middle of the door on the wall's inner face;
 *  `out` is the way into the hall (unit vector), `back` where the lead stands when coming back through it. */
export interface WallDoor { to: SceneId; label: string; x: number; z: number; axis: 'x' | 'z'; out: [number, number]; back: { x: number; z: number; yaw: number }; color: string }
export const WALL_DOORS: WallDoor[] = [
  { to: 'break', label: '休息區', x: 8.0, z: -6.0 + WALL_T / 2, axis: 'x', out: [0, 1], back: { x: 8.0, z: -4.8, yaw: Math.PI }, color: '#8fd16a' },
  { to: 'toilet', label: '廁所', x: 11.6 - WALL_T / 2, z: 4.6, axis: 'z', out: [-1, 0], back: { x: 10.4, z: 4.6, yaw: Math.PI / 2 }, color: '#6fa8dc' },
]
export const DOOR_W = 0.8
export const DOOR_H = 1.3

/** Where the overview camera circles before start: the middle of the building (and dock). */
export const OVERVIEW_CENTER = { x: 1.8, z: 0.8 }

/** Player start: inside the war-room door, facing west across the machine rows (yaw π/2 = facing −x). */
export const START = { x: 5.5, z: 0, yaw: Math.PI / 2 }

/** Status lamp: on a post above the machine (presses: over the back panel), well above the figure's head. */
export const LAMP_POST = 0.5
export function lampPos(p: { h: number; d: number; line: string }): [number, number] {
  return [p.h + LAMP_POST + 0.04, p.line === 'CURE' ? -p.d * 0.15 : 0]
}

export interface Box { x0: number; x1: number; z0: number; z1: number }
const PAD = 0.04
const H = WALL_T / 2
/** A wall along x (at z) or along z (at x) from a to b, with doorway gaps [g0, g1] left open. */
export function wall(axis: 'x' | 'z', at: number, a: number, b: number, gaps: [number, number][] = []): Box[] {
  const out: Box[] = []
  let s = a
  for (const [g0, g1] of [...gaps].sort((m, n) => m[0] - n[0])) { if (g0 > s) out.push(seg(axis, at, s, g0)); s = Math.max(s, g1) }
  if (b > s) out.push(seg(axis, at, s, b))
  return out
}
const seg = (axis: 'x' | 'z', at: number, a: number, b: number): Box =>
  axis === 'x' ? { x0: a, x1: b, z0: at - H, z1: at + H } : { x0: at - H, x1: at + H, z0: a, z1: b }

const { x0: BX0, x1: BX1, z0: BZ0, z1: BZ1 } = BUILDING
const R = ROOMS
/** Wall segments (also what World draws for the inner walls). Doorways are the gaps. */
export const OUTER_WALLS = {
  north: wall('x', BZ0, BX0, BX1), south: wall('x', BZ1, BX0, BX1, [[DOOR.x0, DOOR.x1]]),
  west: wall('z', BX0, BZ0, BZ1), east: wall('z', BX1, BZ0, BZ1),
}
export const INNER_WALLS: { box: Box; kind: 'solid' | 'glass' }[] = [
  // war room: glass to the lobby (north) and the loading area (south), each with a doorway; glass on the factory
  // side (doorway at the C/D aisle); the solid screen wall to the staff corner
  ...wall('x', R.warRoom.z0, ROOM_X, BX1, [[5.3, 6.3]]).map((box) => ({ box, kind: 'glass' as const })),
  ...wall('z', ROOM_X, R.warRoom.z0, R.warRoom.z1, [[-0.6, 0.6]]).map((box) => ({ box, kind: 'glass' as const })),
  ...wall('x', R.warRoom.z1, ROOM_X, R.staff.x0, [[5.3, 6.3]]).map((box) => ({ box, kind: 'glass' as const })),
  ...wall('z', R.staff.x0, R.warRoom.z0, R.warRoom.z1, [[1.6, 2.4]]).map((box) => ({ box, kind: 'solid' as const })),
]

export const pad = (b: Box, p = PAD): Box => ({ x0: b.x0 - p, x1: b.x1 + p, z0: b.z0 - p, z1: b.z1 + p })
export const around = (x: number, z: number, w: number, d: number): Box => ({ x0: x - w / 2, x1: x + w / 2, z0: z - d / 2, z1: z + d / 2 })
/** Dock props (crates, forklift) the lead bumps into. */
export const DOCK_PROPS = { crates: around(8.35, 7.2, 0.7, 0.9), forklift: around(6.0, 7.35, 0.6, 0.95) }
/** Pallets of crates in the loading area: [x, z, crates]. */
export const PALLETS: [number, number, number][] = [[5.3, 4.7, 4], [9.2, 3.2, 6]]
export const COLLIDERS: Box[] = [
  ...PLACED.map((p) => pad({ x0: p.x - p.w / 2, x1: p.x + p.w / 2, z0: p.z - p.d / 2, z1: p.z + p.d / 2 })),
  around(DESK.x, DESK.z, DESK.w, DESK.d),
  around(SCREEN.x - 0.05, SCREEN.z, 0.2, SCREEN.w + 0.2),
  around(PALLETS[0][0], PALLETS[0][1], 0.7, 0.7), around(PALLETS[1][0], PALLETS[1][1], 0.7, 0.7),
  DOCK_PROPS.crates, DOCK_PROPS.forklift,
  ...Object.values(OUTER_WALLS).flat(), ...INNER_WALLS.map((w) => w.box),
]

export const PLAYER_R = 0.22

/** Is (x, z) a place the lead can stand: inside the building, or out on the loading dock? */
export function walkable(x: number, z: number, r = PLAYER_R) {
  const inside = x >= BX0 + H + r && x <= BX1 - H - r && z >= BZ0 + H + r && z <= BZ1 - H - r
  const dock = x >= DOCK.x0 + r && x <= DOCK.x1 - r && z >= BZ1 - H && z <= DOCK.z1 - r
  return inside || dock
}

/** Push a circle (x, z, r) out of every box (machines, furniture, walls) and keep it in the building or on the dock. */
export function collide(x: number, z: number, r = PLAYER_R): [number, number] {
  let [px, pz] = pushOut(COLLIDERS, x, z, r)
  // hard bounds: the building's inner face, or the dock outside the doors
  if (pz > BZ1 - H - r && px > DOCK.x0 + r && px < DOCK.x1 - r) {
    pz = Math.min(pz, DOCK.z1 - r)
  } else {
    px = Math.min(BX1 - H - r, Math.max(BX0 + H + r, px))
    pz = Math.min(BZ1 - H - r, Math.max(BZ0 + H + r, pz))
  }
  return [px, pz]
}

/** Push a circle (x, z, r) out of every box. */
export function pushOut(boxes: Box[], x: number, z: number, r: number): [number, number] {
  let px = x
  let pz = z
  for (const b of boxes) {
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
/** The one red / amber / green of the whole 3D view (lamps, shells, rings, tags), a step darker than before so they hold
 *  their shape on a projector: red #c0352a (the schedule draft's --alarm), amber #e08a00, green #2f9e44 (its --accent
 *  family). NVIDIA green #76b900 is kept for Agent Nemo's success moments only. */
export const LIGHT_HEX: Record<Light, string> = { green: '#2f9e44', amber: '#e08a00', red: '#c0352a', grey: '#8a8f96' }
/** Work in progress that is not a status (goods 備料中 / 生產中, a 補料 task): a neutral blue-grey, so amber in the hall
 *  means only 注意 / 回溫中. */
export const WORK_HEX = '#7f93a8'

/** Line colours: the dashboard's line hues, softened to pastels for the toy factory. */
export const LINE_HEX: Record<Line, string> = {
  A: '#8fbf5a', B: '#6fa8dc', C: '#a993d6', D: '#f29a6b', BOND: '#e8c15a', CURE: '#e07a74',
}

export type Near = { kind: 'machine'; id: string } | { kind: 'desk' } | { kind: 'door'; to: SceneId; label: string } | null
export const nearKey = (n: Near) => !n ? '' : n.kind === 'machine' ? n.id : n.kind === 'door' ? `door-${n.to}` : 'desk'

/** Within reach of a door (centre of the doorway, on the hall side)? */
export function doorDist(d: { x: number; z: number; out: [number, number] }, x: number, z: number) {
  return Math.hypot(x - (d.x + d.out[0] * 0.3), z - (d.z + d.out[1] * 0.3))
}

/** Pressed against the door (walked into it): within its width and touching the wall. */
export function doorTouch(d: { x: number; z: number; axis: 'x' | 'z' }, x: number, z: number) {
  const along = d.axis === 'x' ? Math.abs(x - d.x) : Math.abs(z - d.z)
  const across = d.axis === 'x' ? Math.abs(z - d.z) : Math.abs(x - d.x)
  return along < DOOR_W / 2 - 0.05 && across < WALL_T / 2 + PLAYER_R + 0.04
}

/** Nearest thing within reach: a door, the desk, else the closest machine footprint within 0.75. */
export function nearest(x: number, z: number): Near {
  for (const d of WALL_DOORS) if (doorDist(d, x, z) < 0.75) return { kind: 'door', to: d.to, label: d.label }
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

/** Background workers' walking loops on the production floor (they ignore the player). */
export const WALKER_LOOPS: [number, number][][] = [
  [[-6.8, -3.5], [3.6, -3.5], [3.6, -1.3], [-6.8, -1.3]],
  [[-6.8, 3.25], [3.8, 3.25], [3.8, 5.6], [-6.8, 5.6]],
]
