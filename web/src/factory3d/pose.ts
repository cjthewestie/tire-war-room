// Mutable player pose shared by the controller, the machines (proximity labels) and the HUD.
// Kept outside React state so the 60 fps loop never re-renders the tree.
import { START } from './layout'

export const player = { x: START.x, z: START.z, yaw: START.yaw, pitch: -0.05, moving: false }

export function teleport(x: number, z: number, yaw?: number) {
  player.x = x
  player.z = z
  if (yaw != null) player.yaw = yaw
}
