// Mutable player + camera pose shared by the controller, the machines (proximity labels), the HUD and the test hook.
// Kept outside React state so the 60 fps loop never re-renders the tree.
import { START } from './layout'

/** yaw: the way the figure faces; forward = (−sin yaw, −cos yaw), so yaw 0 faces −z (north). */
export const player = { x: START.x, z: START.z, yaw: START.yaw, moving: false, running: false }

/** Orbit camera around the figure: yaw 0 sits south of it looking north; pitch is the look-down angle. */
export const cam = { yaw: START.yaw, pitch: 0.8, dist: 14, snap: true }
export const CAM_LIMITS = { pitchMin: 0.38, pitchMax: 1.2, distMin: 4.5, distMax: 24 }

/** Move the figure; with a yaw it also turns the figure and swings the camera behind it, without easing. */
export function teleport(x: number, z: number, yaw?: number) {
  player.x = x
  player.z = z
  if (yaw != null) { player.yaw = yaw; cam.yaw = yaw }
  cam.snap = true
}
