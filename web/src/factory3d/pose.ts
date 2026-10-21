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

/** The guided demo (useGuidedDemo.ts): while `on`, the opening overview ('orbit') is demoCamera.ts's camera, then the
 *  lead walks in first person ('walk', demoWalk.ts) with Player.tsx's own eye camera; either way
 *  every free camera input — mouse, wheel, WASD, gamepad sticks — is ignored (CJ: 「demo按鍵按下去就只能按照流程跑」). */
export const director = {
  on: false, mode: 'orbit' as 'orbit' | 'walk',
  /** people closer than this to the camera are hidden (the demo's doorway view of 7 號機: no staff in the foreground) */
  clearRadius: 0,
}

/** Which camera and which space: first person is the default view; the factory hall the default space. */
export const view = { mode: 'first' as 'first' | 'third', scene: 'factory' as SceneId }

/** Move the lead; with a yaw it also turns the lead (and the overview camera, to camYaw), without easing. */
export function teleport(x: number, z: number, yaw?: number, camYaw?: number) {
  player.x = x
  player.z = z
  if (yaw != null) { player.yaw = yaw; player.pitch = 0; cam.yaw = camYaw ?? yaw }
  cam.snap = true
}

/** Phones and tablets (no mouse): the overview is the default view and on-screen controls stand in for the keys. */
export const TOUCH_DEVICE = typeof matchMedia !== 'undefined' && matchMedia('(pointer: coarse)').matches
/** On-screen stick (TouchControls): forward / strafe, each −1…1, read by the controller like W/S and A/D. */
export const stick = { fw: 0, st: 0 }
