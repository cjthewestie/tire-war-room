// The patrol robot's routes (PatrolRobot.tsx): the aisles it drives, its loop and inspection stops, and the path along
// the aisles to a machine. Pure data and geometry, plus the robot's live pose (read by the demo camera and the e2e hook).
import type { Placed } from './layout'

/** Aisle centre lines (z) between the rows, and the two cross corridors (x) at the ends of the rows. */
export const AISLE: Record<string, number> = { A: -3.5, B: -1.25, C: 0.9, D: 0.9, BOND: 0.9, CURE: 3.2 }
export const EAST_X = 4.2
export const WEST_X = -7.45
/** The patrol loop: east along the C/D aisle, down the east corridor, west along the 硫化 aisle, back up the west end. */
export const LOOP: [number, number][] = [[WEST_X, 0.9], [EAST_X, 0.9], [EAST_X, 3.2], [WEST_X, 3.2]]
/** Where it stops on the loop to inspect: [x, z, machine it looks at]. */
export const STOPS: { x: number; z: number; id: string }[] = [
  { x: -4.6, z: 0.9, id: 'C-EXTR' }, { x: -1.3, z: 0.9, id: 'C-WRAP' },
  { x: 2.1, z: 3.2, id: 'CURE-10' }, { x: -1.6, z: 3.2, id: 'CURE-07' }, { x: -5.4, z: 3.2, id: 'CURE-02' },
]

/** Live pose, for the demo camera and the e2e hook. */
export const robot = { x: WEST_X, z: 0.9, yaw: 0, mode: 'patrol' as 'patrol' | 'divert' | 'parked' | 'return', target: null as string | null }

/** The stand point in the aisle in front of a machine. */
export function standPoint(p: Placed): [number, number] {
  return [Math.max(WEST_X + 0.3, Math.min(EAST_X - 0.3, p.x)), AISLE[p.line] ?? 0.9]
}

/** Along the aisles from (x, z) to (tx, tz): straight when on the same aisle, else via a cross corridor. */
export function route(x: number, z: number, tx: number, tz: number): [number, number][] {
  const onEast = Math.abs(x - EAST_X) < 0.05, onWest = Math.abs(x - WEST_X) < 0.05
  if (!onEast && !onWest && Math.abs(z - tz) < 0.05) return [[tx, tz]]
  const cx = onWest ? WEST_X : onEast ? EAST_X : (Math.abs(x - WEST_X) < Math.abs(x - EAST_X) && tx < -2 ? WEST_X : EAST_X)
  const out: [number, number][] = []
  if (Math.abs(x - cx) > 0.01) out.push([cx, z])
  out.push([cx, tz], [tx, tz])
  return out
}

