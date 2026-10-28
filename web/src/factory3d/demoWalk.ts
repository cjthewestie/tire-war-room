// The guided demo as a first-person walk (CJ, 2026-10-22: 「一開始俯視角可以 接下來要帶到第一人視角 走到電腦前點開戰情室
// … 有事件發生然後核准往前跑過去看員工處理跟小機器人互動 然後回來電腦看戰情室agent頁面 然後pitch 站到白板前打開」).
// After the opening overview, the lead walks (never teleports) between a few stations: the war-room desk, the
// doorway looking out at 7 號機, the aisle in front of 7 號機, back to the desk, and the whiteboard beside the glass.
// walker.update() moves `player` along the path each frame (Player.tsx calls it while the demo walks), easing in and
// out, turning toward where it goes, then turning to face what the beat is about.
import type { Shot } from '../demo/cues'
import { BOARD_STAND, DESK, PLACED_BY_ID } from './layout'
import { player } from './pose'

export interface Stand { x: number; z: number; yaw: number; pitch: number }
type P = [number, number]

const WALK = 4.2, RUN = 6.5   // brisk: no walk between beats takes much over 2 s
const wrap = (a: number) => Math.atan2(Math.sin(a), Math.cos(a))
/** yaw that faces from (x, z) toward (tx, tz) (forward = (−sin yaw, −cos yaw)) */
export const yawToward = (x: number, z: number, tx: number, tz: number) => Math.atan2(-(tx - x), -(tz - z))

/** The stations. 7 號機 is CURE-07, in the 硫化 row; the lead stands in the aisle north of it. */
const P7 = PLACED_BY_ID['CURE-07']
/** Where the robot stops to meet the lead: in the 硫化 aisle, a little east of 7 號機. */
export const ROBOT_MEET: [number, number] = [P7.x + 1.6, 3.2]
export const STATIONS = {
  desk: { x: DESK.x - 0.63, z: DESK.z, yaw: -Math.PI / 2, pitch: -0.3 } as Stand,
  doorway: { x: 5.0, z: 0.15, yaw: yawToward(5.0, 0.15, P7.x, P7.z), pitch: -0.06 } as Stand,
  // auto-play's leave (CJ 10-22: 「退出電腦往後看，第一人稱」): a step back from the desk, turned round to the factory
  lookout: { x: 7.05, z: 0.1, yaw: yawToward(7.05, 0.1, P7.x, P7.z), pitch: -0.05 } as Stand,
  // a step back from the press, looking a little up: the press, its lamp and ring, and the worker in one view
  press7: { x: P7.x, z: 2.55, yaw: Math.PI, pitch: 0.16 } as Stand,
  // the same spot, turned to the robot that comes over along the aisle (it plays its show facing the lead)
  robot: { x: P7.x, z: 2.55, yaw: yawToward(P7.x, 2.55, P7.x + 1.6, 3.2), pitch: -0.12 } as Stand,
  board: { x: BOARD_STAND.x, z: BOARD_STAND.z, yaw: BOARD_STAND.yaw, pitch: 0.06 } as Stand,
}
export type StationId = keyof typeof STATIONS

/** Waypoints between stations that stay clear of the desk, the glass walls and the machines (through the war room's
 *  west doorway, down the east corridor at x 4.2, along the 硫化 aisle). */
const VIA: Record<string, P[]> = {
  'desk>doorway': [[6.4, 0.1]],
  'desk>lookout': [],
  'lookout>press7': [[6.4, 0.1], [5.0, 0.15], [4.35, 0.5], [4.2, 2.6], [4.0, 2.7]],
  'lookout>desk': [],
  'doorway>press7': [[4.35, 0.5], [4.2, 2.6], [4.0, 2.7]],
  'press7>desk': [[4.0, 2.7], [4.2, 2.6], [4.35, 0.5], [5.0, 0.15], [6.4, 0.1]],
  'robot>desk': [[4.0, 2.7], [4.2, 2.6], [4.35, 0.5], [5.0, 0.15], [6.4, 0.1]],
  'press7>robot': [],
  'robot>board': [[4.0, 2.7], [4.2, 2.6], [4.35, 0.5], [5.0, 0.15], [6.6, 0.0]],
  'doorway>desk': [[6.4, 0.1]],
  'desk>board': [[7.55, -0.05]],
  'press7>board': [[4.0, 2.7], [4.2, 2.6], [4.35, 0.5], [5.0, 0.15], [6.6, 0.0]],
  'doorway>board': [[6.6, 0.0]],
}

/** Which station each cue walks to (null: stay where the lead is, e.g. the approvals at the doorway). */
export function stationFor(cueId: string, shot: Shot): StationId | null {
  if (shot.kind === 'establish') return null
  return ({ rush: 'desk', console: 'desk', event: 'doorway', worker: 'press7', robot: 'robot', agent: 'desk', pitch: 'board', warn: 'board', end: 'board' } as Record<string, StationId>)[cueId] ?? null
}

class Walker {
  path: P[] = []
  goal: Stand | null = null
  at: StationId | null = null
  speed = 0
  run = false
  done = true
  /** auto-play: a calmer walk and turn (CJ: 「要順暢」); kept across reset() */
  calm = false
  /** walk to a station along its waypoints (from wherever the lead is now) */
  go(to: StationId, opts: { run?: boolean } = {}) {
    const g = STATIONS[to]
    const via = this.at ? VIA[`${this.at}>${to}`] ?? [] : []
    this.path = [...via, [g.x, g.z]]
    this.goal = g
    this.at = to
    this.run = !!opts.run
    this.done = false
  }
  reset() { this.path = []; this.goal = null; this.at = null; this.speed = 0; this.done = true }
  /** One frame; returns whether the lead is walking (for the head-bob and the walk cycle). */
  update(dt: number): boolean {
    if (!this.goal) return false
    const top = this.calm ? 1.4 : this.run ? RUN : WALK   // auto-play: a calm walking pace (CJ: 「不要急」)
    if (this.path.length) {
      const [gx, gz] = this.path[0]
      const dx = gx - player.x, dz = gz - player.z, len = Math.hypot(dx, dz)
      const want = Math.atan2(-dx, -dz)
      player.yaw += wrap(want - player.yaw) * Math.min(1, dt * (this.calm ? 4 : 8))
      player.pitch += (0 - player.pitch) * Math.min(1, dt * 3)
      // ease in from standing, slow into the last point
      const left = this.path.reduce((a, p, i) => a + (i === 0 ? len : Math.hypot(p[0] - this.path[i - 1][0], p[1] - this.path[i - 1][1])), 0)
      const target = Math.min(top, Math.max(0.6, left * 2.4))
      this.speed += (target - this.speed) * Math.min(1, dt * 6)
      const step = Math.min(len, this.speed * dt)
      if (len < 0.02) { this.path.shift(); return true }
      player.x += (dx / len) * step; player.z += (dz / len) * step
      player.running = this.run && this.speed > 2
      return true
    }
    // arrived: turn to face what this station is about
    this.speed = 0
    player.running = false
    const dy = wrap(this.goal.yaw - player.yaw), dp = this.goal.pitch - player.pitch
    player.yaw += dy * Math.min(1, dt * (this.calm ? 3.5 : 6))
    player.pitch += dp * Math.min(1, dt * (this.calm ? 3.5 : 6))
    if (Math.abs(dy) < 0.05 && Math.abs(dp) < 0.05) this.done = true
    return false
  }
}

export const walker = new Walker()
