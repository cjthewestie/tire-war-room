// The spaces the lead can be in: the factory hall (layout.ts), and two separate small spaces reached through doors
// on the hall's walls: the 休息區 (half court) and the 廁所. Each space has its own floor plan, colliders, the exit
// door back to the hall, and where the lead appears. Pure data plus collision, like layout.ts.
import {
  PLAYER_R, START, WALL_DOORS, WALL_T, around, doorTouch, collide as factoryCollide, nearest as factoryNearest, pad, pushOut, wall, walkable as factoryWalkable,
  type Box, type Near, type SceneId,
} from './layout'

export interface Room {
  id: Exclude<SceneId, 'factory'>; label: string
  /** inner floor rectangle (walls sit on its edges) */
  floor: Box
  /** exit door on the south wall, at x = door */
  door: number
  walls: Box[]; colliders: Box[]
  spawn: { x: number; z: number; yaw: number }
}

const H = WALL_T / 2
function room(id: Room['id'], label: string, floor: Box, door: number, props: Box[], yaw = 0): Room {
  const walls = [
    ...wall('x', floor.z0, floor.x0, floor.x1), ...wall('x', floor.z1, floor.x0, floor.x1),
    ...wall('z', floor.x0, floor.z0, floor.z1), ...wall('z', floor.x1, floor.z0, floor.z1),
  ]
  return { id, label, floor, door, walls, colliders: [...walls, ...props.map((b) => pad(b))], spawn: { x: door, z: floor.z1 - 1.1, yaw } }
}

/** 休息區: a small yard room, 8 × 6. Half court in the north-east with the hoop on the north wall; bench, vending
 *  machine and a plant on the west side; big windows on the north and west walls. */
export const BREAK = {
  court: { x0: -0.6, x1: 3.85, z0: -2.85, z1: 0.9, hoopX: 1.6 },
  bench: { x: -3.3, z: -0.4, w: 0.36, d: 1.4 },
  vending: { x: -3.55, z: -2.3 },
  plant: { x: 3.6, z: 2.6 },
  table: { x: -2.2, z: 1.8 },
}
export const BREAK_ROOM = room('break', '休息區', { x0: -4, x1: 4, z0: -3, z1: 3 }, 0, [
  around(BREAK.bench.x, BREAK.bench.z, BREAK.bench.w, BREAK.bench.d),
  around(BREAK.vending.x, BREAK.vending.z, 0.42, 0.55),
  around(BREAK.court.hoopX, -3 + 0.35, 0.3, 0.5),
  around(BREAK.plant.x, BREAK.plant.z, 0.4, 0.4),
  around(BREAK.table.x, BREAK.table.z, 0.8, 0.8),
])

/** 廁所: 5 × 4. Three stalls along the north wall, sinks with a mirror on the east wall, a plant in the south-west corner. */
export const TOILET = {
  stalls: [-1.8, -0.8, 0.2].map((x) => ({ x, w: 1.0, d: 1.1 })),
  sinks: [-0.6, 0.4, 1.4].map((z) => ({ x: 2.25, z })),
}
export const TOILET_ROOM = room('toilet', '廁所', { x0: -2.5, x1: 2.5, z0: -2, z1: 2 }, -1.2, [
  // stall partitions + doors: one box over the row, so the lead stays in the aisle
  { x0: -2.5, x1: 0.7, z0: -2, z1: -0.9 },
  ...TOILET.sinks.map((s) => around(s.x, s.z, 0.5, 0.5)),
  around(-2.2, 1.7, 0.4, 0.4),
], -0.6)   // arrive looking toward the mirror and the stalls

export const ROOMS_BY_ID: Record<Room['id'], Room> = { break: BREAK_ROOM, toilet: TOILET_ROOM }

export interface Space {
  id: SceneId
  collide(x: number, z: number, r?: number): [number, number]
  walkable(x: number, z: number, r?: number): boolean
  nearest(x: number, z: number): Near
  /** the door the lead is pressed against (walked into), if any */
  touch(x: number, z: number): SceneId | null
  spawn: { x: number; z: number; yaw: number }
}

function roomSpace(r: Room): Space {
  const { x0, x1, z0, z1 } = r.floor
  const inside = (x: number, z: number, rr: number) => x >= x0 + H + rr && x <= x1 - H - rr && z >= z0 + H + rr && z <= z1 - H - rr
  return {
    id: r.id,
    collide(x, z, rr = PLAYER_R) {
      const [px, pz] = pushOut(r.colliders, x, z, rr)
      return [Math.min(x1 - H - rr, Math.max(x0 + H + rr, px)), Math.min(z1 - H - rr, Math.max(z0 + H + rr, pz))]
    },
    walkable: (x, z, rr = PLAYER_R) => inside(x, z, rr),
    nearest: (x, z) => (Math.hypot(x - r.door, z - (z1 - 0.3)) < 0.75 ? { kind: 'door', to: 'factory', label: '回到工廠' } : null),
    touch: (x, z) => (doorTouch({ x: r.door, z: z1, axis: 'x' }, x, z) ? 'factory' : null),
    spawn: r.spawn,
  }
}

export const SPACES: Record<SceneId, Space> = {
  factory: {
    id: 'factory', collide: factoryCollide, walkable: factoryWalkable, nearest: factoryNearest, spawn: START,
    touch: (x, z) => WALL_DOORS.find((d) => doorTouch(d, x, z))?.to ?? null,
  },
  break: roomSpace(BREAK_ROOM),
  toilet: roomSpace(TOILET_ROOM),
}

/** Where the lead stands on arriving in `to` from `from`: back in the hall just inside the matching door, or at a
 *  room's own door. */
export function arrival(to: SceneId, from: SceneId) {
  if (to === 'factory') return WALL_DOORS.find((d) => d.to === from)?.back ?? START
  return ROOMS_BY_ID[to].spawn
}
