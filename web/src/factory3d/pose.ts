// Mutable player + camera pose shared by the controller, the machines (proximity labels), the HUD and the test hook.
// Kept outside React state so the 60 fps loop never re-renders the tree.
import { START, type SceneId } from './layout'

/** yaw: the way the lead faces; forward = (−sin yaw, −cos yaw), so yaw 0 faces −z (north). pitch: first-person look
 *  up (+) / down (−). */
export const player = { x: START.x, z: START.z, yaw: START.yaw, pitch: 0, moving: false, running: false }

/** Orbit camera for the overview (V): yaw 0 sits south of the lead looking north; pitch is the look-down angle. */
export const cam = { yaw: START.yaw, pitch: 0.85, dist: 20, snap: true }
export const CAM_LIMITS = { pitchMin: 0.38, pitchMax: 1.2, distMin: 4.5, distMax: 36 }
/** Before start: the whole building from far out, slowly circling its centre. */
export const OVERVIEW_DIST = 36

/** Which camera and which space: first person is the default view; the factory hall the default space. */
export const view = { mode: 'first' as 'first' | 'third', scene: 'factory' as SceneId }

/** Move the lead; with a yaw it also turns the lead (and the overview camera, to camYaw), without easing. */
export function teleport(x: number, z: number, yaw?: number, camYaw?: number) {
  player.x = x
  player.z = z
  if (yaw != null) { player.yaw = yaw; player.pitch = 0; cam.yaw = camYaw ?? yaw }
  cam.snap = true
}
