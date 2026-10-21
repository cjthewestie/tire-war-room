// The guided demo's one-take camera (CJ, 2026-10-22: 「固定視角的意思是幫我安排連貫的視角」): while the demo runs, the
// camera is never handed to the player and never cuts. Each beat names a shot (cues.ts Shot); the camera flies from
// wherever it is now to that shot along a Catmull-Rom spline that rises over the lines and the walls (so it never
// passes through a machine, a wall or the whiteboard), eased in and out over 1.5–2.5 s, with the look-at point blending
// the same way. On a shot it keeps a slow drift (a small orbit and breathing distance), so a held frame stays alive.
// One lens (the overview's 32°) the whole way; the incident and the worker shots are the only zoom-ins.
// Player.tsx calls update() every frame while `director.on` (pose.ts) instead of its own camera.
import * as THREE from 'three'
import type { Shot } from '../demo/cues'
import { PLACED_BY_ID, WHITEBOARD } from './layout'
import { robot } from './patrol'

/** Orbit-style shot: look at (x, y, z) from yaw / pitch / dist (yaw 0 = camera south of the point looking north). */
interface Frame { x: number; y: number; z: number; yaw: number; pitch: number; dist: number }
export interface Pose { pos: THREE.Vector3; look: THREE.Vector3 }

const ease = (u: number) => (u < 0.5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2)   // ease in-out (cubic)
const lerp = (a: number, b: number, u: number) => a + (b - a) * u
/** Height the flight path rises to between shots: over the roof trusses (2.5) and the walls. */
const OVER = 4.6

export const LENS = 32

/** The frame for a shot, `s` seconds after it began (the establishing shot pushes in; every shot drifts). */
export function frameFor(shot: Shot, machine: string | null, s: number, reduced = false): Frame {
  const k = reduced ? 0.4 : 1
  const drift = (amp: number, w: number) => amp * k * Math.sin(s * w)
  if (shot.kind === 'establish') {
    // a wide shot of the whole building, then a slow push along the rows (east → west) as the orders come in
    const u = ease(Math.min(1, s / 14))
    return { x: lerp(1.8, -1.4, u), y: 0.5, z: lerp(0.8, 1.2, u), yaw: lerp(0.62, 0.22, u) + drift(0.03, 0.2), pitch: lerp(0.9, 0.62, u), dist: lerp(32, 16, u) }
  }
  if (shot.kind === 'incident') {
    const p = machine ? PLACED_BY_ID[machine] : null
    if (p) {
      // from the aisle side (presses: from the north), high enough that the beacon pillar and the ring both show
      const north = p.line === 'CURE' || p.line === 'D' || p.line === 'BOND'
      const close = shot.close ? 1 : 0
      return {
        // look above the machine, so it sits low in the frame and its beacon pillar rises up through the shot
        x: p.x, y: lerp(2.1, 1.5, close), z: p.z + (north ? -0.5 : 0.5),
        yaw: (north ? Math.PI : 0) - 0.42 + drift(0.12, 0.23), pitch: lerp(0.5, 0.45, close) + drift(0.02, 0.31),
        dist: lerp(10.5, 6.2, close) * (1 + drift(0.03, 0.17)),
      }
    }
  }
  if (shot.kind === 'robot') {
    return { x: robot.x, y: 0.35, z: robot.z, yaw: Math.PI - 0.5 + drift(0.14, 0.25), pitch: 0.42, dist: 4.6 * (1 + drift(0.04, 0.2)) }
  }
  if (shot.kind === 'whiteboard') {
    // locked on the board: the slide stays readable, only a breath of movement
    return { x: WHITEBOARD.x, y: WHITEBOARD.y, z: WHITEBOARD.z, yaw: WHITEBOARD.yaw + drift(0.015, 0.2), pitch: 0.2, dist: 3.9 }
  }
  // overview (also the incident shot before its machine is known)
  return { x: 0.9, y: 0.5, z: 0.6, yaw: 0.36 + drift(0.1, 0.12), pitch: 0.78 + drift(0.02, 0.19), dist: 22 * (1 + drift(0.025, 0.15)) }
}

export function poseOf(f: Frame): Pose {
  const hr = Math.cos(f.pitch) * f.dist
  return {
    pos: new THREE.Vector3(f.x + Math.sin(f.yaw) * hr, f.y + Math.sin(f.pitch) * f.dist, f.z + Math.cos(f.yaw) * hr),
    look: new THREE.Vector3(f.x, f.y, f.z),
  }
}

/** The camera's route between two poses: up and over (never through the lines, the walls or the board), then down. */
export function routeBetween(from: THREE.Vector3, to: THREE.Vector3): THREE.CatmullRomCurve3 {
  const mid = from.clone().lerp(to, 0.5)
  const d = from.distanceTo(to)
  mid.y = Math.max(from.y, to.y, d > 3 ? OVER : Math.max(from.y, to.y))
  const pts = [from.clone(), mid, to.clone()]
  // leave and arrive along the shot's own height for the last stretch (a soft landing, not a dive)
  if (d > 3) {
    pts.splice(1, 0, from.clone().lerp(mid, 0.35).setY(lerp(from.y, mid.y, 0.6)))
    pts.splice(pts.length - 1, 0, to.clone().lerp(mid, 0.3).setY(lerp(to.y, mid.y, 0.55)))
  }
  return new THREE.CatmullRomCurve3(pts, false, 'centripetal')
}

/** Flight duration: 1.5 s for a short move, up to 2.5 s across the building. */
export const flightTime = (d: number) => Math.min(2.5, Math.max(1.5, 1.4 + d / 18))

interface Flight { curve: THREE.CatmullRomCurve3; fromLook: THREE.Vector3; t0: number; dur: number }

/** The demo camera. setShot() on every beat; update() every frame. */
export class DemoCamera {
  private shot: Shot = { kind: 'overview' }
  private machine: string | null = null
  private key = ''
  private pendingFlight = false
  private shotT0 = 0
  private flight: Flight | null = null
  private look = new THREE.Vector3()
  private started = false
  reduced = false
  /** test hook: every frame's largest step (to prove there are no cuts) */
  maxStep = 0
  private last = new THREE.Vector3()

  setShot(shot: Shot, machine: string | null) {
    const key = `${shot.kind}|${'close' in shot && shot.close ? 1 : 0}|${shot.kind === 'incident' ? machine ?? '' : ''}`
    this.shot = shot
    this.machine = machine
    if (key === this.key) return
    this.key = key
    this.pendingFlight = true
  }

  /** A new demo: the first frame starts from wherever the camera is (the player's view), without a jump. */
  reset() {
    this.started = false
    this.flight = null
    this.key = ''
    this.maxStep = 0
  }

  update(camera: THREE.PerspectiveCamera, now: number) {
    if (this.pendingFlight || !this.started) {
      this.pendingFlight = false
      this.shotT0 = now
      const target = poseOf(frameFor(this.shot, this.machine, 0, this.reduced))
      if (!this.started) {
        this.started = true
        const dir = new THREE.Vector3()
        camera.getWorldDirection(dir)
        this.look.copy(camera.position).addScaledVector(dir, 8)
        this.last.copy(camera.position)
      }
      const from = camera.position.clone()
      const d = from.distanceTo(target.pos)
      this.flight = { curve: routeBetween(from, target.pos), fromLook: this.look.clone(), t0: now, dur: flightTime(d) }
    }
    const s = now - this.shotT0
    const target = poseOf(frameFor(this.shot, this.machine, s, this.reduced))
    let pos = target.pos
    let look = target.look
    const f = this.flight
    if (f) {
      const u = Math.min(1, (now - f.t0) / f.dur)
      const e = ease(u)
      // the route ends where the (drifting, maybe moving) shot is now, so the landing has no step
      const pts = f.curve.points
      const end = pts[pts.length - 1]
      const shift = target.pos.clone().sub(end)
      end.copy(target.pos)
      if (pts.length > 3) pts[pts.length - 2].addScaledVector(shift, 1)
      pos = f.curve.getPoint(e)
      look = f.fromLook.clone().lerp(target.look, e)
      if (u >= 1) this.flight = null
    }
    this.maxStep = Math.max(this.maxStep, pos.distanceTo(this.last))
    this.last.copy(pos)
    camera.position.copy(pos)
    this.look.copy(look)
    camera.lookAt(look)
    if (Math.abs(camera.fov - LENS) > 0.01) { camera.fov = LENS; camera.near = 0.1; camera.updateProjectionMatrix() }
  }

  get flying() { return !!this.flight }
  get lookAt() { return this.look.clone() }
}

export const demoCam = new DemoCamera()
