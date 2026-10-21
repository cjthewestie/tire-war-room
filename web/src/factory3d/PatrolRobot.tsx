// The patrol inspection vehicle (CJ, 2026-10-22: 「我的工廠要有一台巡邏的巡檢機器車上面有機器手臂」): a small AGV with an
// articulated arm (turret, two links, a gripper with a sensor head), built from the kit's primitives. It drives a loop
// through the aisles between the machine rows (C/D and D/硫化), stopping now and then to raise the arm and point its
// sensor at a machine. When an incident opens, it drives to that machine along the aisles, parks, points the arm at
// it and sweeps a scanning light until the event closes, then rejoins the loop. Visual only: no tool, no plant write.
// One group, no shadow casting (a soft blob underneath instead), so it costs next to nothing per frame.
import { useFrame } from '@react-three/fiber'
import { useEffect, useMemo, useRef } from 'react'
import * as THREE from 'three'
import { box, cyl, mesh, rbox } from './kit'
import { PLACED_BY_ID } from './layout'
import { EAST_X, LOOP, PLACED_IDS, STOPS, WEST_X, robot, route, showStep, standPoint } from './patrol'

const SPEED = 0.62
const INSPECT_S = 3.4

function build() {
  const root = new THREE.Group()
  root.rotation.order = 'YXZ'   // heading first, then the bow tips the nose
  const Y = '#f2c14e', CREAM = '#f3ead8', DARK = '#4d535b', STEEL = '#a9b2ba'
  // blob shadow
  const blob = new THREE.Mesh(new THREE.CircleGeometry(0.36, 20), new THREE.MeshBasicMaterial({ color: '#000000', transparent: true, opacity: 0.18, depthWrite: false }))
  blob.rotation.x = -Math.PI / 2
  blob.position.y = 0.012
  root.add(blob)
  // chassis (front = +z): yellow body, cream deck, dark bumper strip, four wheels, a lidar puck at the front
  root.add(rbox(0.42, 0.14, 0.58, Y, 0, 0.06, 0, 0.07))
  root.add(rbox(0.36, 0.03, 0.5, CREAM, 0, 0.2, 0, 0.06))
  root.add(box(0.44, 0.04, 0.05, DARK, 0, 0.09, 0.3))
  root.add(box(0.44, 0.04, 0.05, DARK, 0, 0.09, -0.3))
  for (const [x, z] of [[-0.21, 0.19], [0.21, 0.19], [-0.21, -0.19], [0.21, -0.19]]) {
    const w = cyl(0.065, 0.065, 0.05, '#2b2f35', x, 0.065, z, 12)
    w.rotation.z = Math.PI / 2
    root.add(w)
  }
  root.add(cyl(0.05, 0.055, 0.05, DARK, 0, 0.255, 0.19, 12))
  const lidar = cyl(0.035, 0.035, 0.025, '#22262c', 0, 0.29, 0.19, 12)
  root.add(lidar)
  // beacon on a short mast at the back: green on patrol, amber blinking at an incident
  root.add(cyl(0.012, 0.012, 0.16, STEEL, 0.13, 0.3, -0.2, 6))
  const beaconMat = new THREE.MeshStandardMaterial({ color: '#2d6b3a', emissive: '#3fcf5a', emissiveIntensity: 1.2, flatShading: true })
  const beacon = mesh(new THREE.IcosahedronGeometry(0.035, 0), beaconMat, 0.13, 0.39, -0.2)
  root.add(beacon)
  // a small screen on the back face
  const screenMat = new THREE.MeshStandardMaterial({ color: '#0d1a12', emissive: '#3fcf5a', emissiveIntensity: 0.4 })
  const screen = new THREE.Mesh(new THREE.PlaneGeometry(0.2, 0.07), screenMat)
  screen.position.set(0, 0.13, -0.296)
  screen.rotation.y = Math.PI
  root.add(screen)

  // arm: turret (yaw) → shoulder (pitch) → link 1 → elbow (pitch) → link 2 → wrist (roll) → gripper + sensor
  const turret = new THREE.Group()
  turret.position.set(0, 0.215, -0.04)
  root.add(turret)
  turret.add(cyl(0.085, 0.1, 0.06, DARK, 0, 0.03, 0, 14))
  turret.add(cyl(0.06, 0.07, 0.06, Y, 0, 0.09, 0, 12))
  const shoulder = new THREE.Group()
  shoulder.position.y = 0.13
  turret.add(shoulder)
  const joint = (r: number) => { const j = cyl(r, r, 0.09, STEEL, 0, 0, 0, 10); j.rotation.z = Math.PI / 2; return j }
  shoulder.add(joint(0.04))
  shoulder.add(rbox(0.06, 0.3, 0.06, Y, 0, 0, 0, 0.02))
  const elbow = new THREE.Group()
  elbow.position.y = 0.3
  shoulder.add(elbow)
  elbow.add(joint(0.033))
  elbow.add(rbox(0.05, 0.24, 0.05, CREAM, 0, 0, 0, 0.018))
  const wrist = new THREE.Group()
  wrist.position.y = 0.24
  elbow.add(wrist)
  wrist.add(cyl(0.032, 0.028, 0.04, DARK, 0, 0.02, 0, 10))
  // gripper: two fingers, and the sensor head between them (lens looks along the arm, +y of the wrist)
  const fingerL = box(0.015, 0.07, 0.03, STEEL, -0.03, 0.075, 0)
  const fingerR = box(0.015, 0.07, 0.03, STEEL, 0.03, 0.075, 0)
  wrist.add(fingerL, fingerR)
  wrist.add(cyl(0.02, 0.024, 0.05, '#22262c', 0, 0.065, 0, 10))
  const lensMat = new THREE.MeshStandardMaterial({ color: '#0a2a40', emissive: '#6fd3ff', emissiveIntensity: 0.5 })
  wrist.add(mesh(new THREE.CircleGeometry(0.016, 12).rotateX(-Math.PI / 2), lensMat, 0, 0.091, 0))
  // the scanning light: a soft cone out of the lens (apex at the lens), additive, only while it inspects
  const coneGeo = new THREE.ConeGeometry(0.16, 0.7, 20, 1, true)
  coneGeo.translate(0, -0.35, 0)
  coneGeo.rotateX(Math.PI)
  const coneMat = new THREE.MeshBasicMaterial({ color: '#7fe0ff', transparent: true, opacity: 0, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide, toneMapped: false })
  const cone = new THREE.Mesh(coneGeo, coneMat)
  cone.position.y = 0.092
  cone.renderOrder = 4
  wrist.add(cone)

  root.traverse((o) => { if ((o as THREE.Mesh).isMesh) { o.castShadow = false; o.receiveShadow = false } })
  return { root, turret, shoulder, elbow, wrist, fingerL, fingerR, cone, coneMat, lensMat, beacon, beaconMat, screenMat, lidar }
}
type Rig = ReturnType<typeof build>

/** Arm poses: [shoulder, elbow] pitch (positive leans forward). */
const FOLD: [number, number] = [-0.5, 2.3]
const RAISE: [number, number] = [0.05, 0.25]
const POINT: [number, number] = [0.55, 1.15]
/** the robot beat: arm up high (waving), and folded forward (the bow) */
const WAVE: [number, number] = [-0.15, 0.35]
const BOW: [number, number] = [0.9, 1.6]
const lerp = (a: number, b: number, u: number) => a + (b - a) * u
const ease = (u: number) => u * u * (3 - 2 * u)
const wrapA = (a: number) => Math.atan2(Math.sin(a), Math.cos(a))

/** Where the arm is in an inspect motion at u = 0…1: raise, point at the machine, hold, lower. */
function inspectPose(u: number): { a: [number, number]; light: number } {
  if (u < 0.2) { const k = ease(u / 0.2); return { a: [lerp(FOLD[0], RAISE[0], k), lerp(FOLD[1], RAISE[1], k)], light: 0 } }
  if (u < 0.35) { const k = ease((u - 0.2) / 0.15); return { a: [lerp(RAISE[0], POINT[0], k), lerp(RAISE[1], POINT[1], k)], light: k } }
  if (u < 0.75) return { a: POINT, light: 1 }
  const k = ease((u - 0.75) / 0.25)
  return { a: [lerp(POINT[0], FOLD[0], k), lerp(POINT[1], FOLD[1], k)], light: 1 - k }
}

/** `incident`: the machine of the open event it should go and look at (null = patrol). */
export function PatrolRobot({ incident }: { incident: string | null }) {
  const rig = useMemo<Rig>(() => build(), [])
  const s = useRef({
    path: [] as [number, number][], loopI: 1, stopDone: new Set<number>(), inspect: -1, inspectFor: '' as string,
    arm: [...FOLD] as [number, number], yaw: Math.PI / 2, light: 0, target: null as string | null,
    show: -1, showFor: '' as string, bow: 0,
  })
  const target = incident && PLACED_BY_ID[incident] ? incident : null

  // a new incident (or it closed): plan the drive there, or back to the loop
  useEffect(() => {
    const st = s.current
    if (target === st.target) return
    st.target = target
    st.inspect = -1
    if (target) {
      const [tx, tz] = standPoint(PLACED_BY_ID[target])
      st.path = route(robot.x, robot.z, tx, tz)
      robot.mode = 'divert'
    } else {
      // rejoin at the loop corner on the C/D aisle's east end (or the 硫化 aisle's, whichever aisle it is on)
      const back: [number, number] = Math.abs(robot.z - 3.2) < 0.05 ? [WEST_X, 3.2] : [EAST_X, 0.9]
      st.path = route(robot.x, robot.z, back[0], back[1])
      st.loopI = back[1] === 3.2 ? 0 : 2
      robot.mode = 'return'
    }
    robot.target = target
  }, [target])

  useFrame(({ clock, camera }, dtRaw) => {
    const dt = Math.min(dtRaw, 0.2)   // real time even at a low frame rate (the projector laptop, headless e2e)
    const t = clock.elapsedTime
    const st = s.current
    const r = rig
    let faceYaw: number | null = null
    let moving = false

    // 0. the demo's robot beat: stop where it is and play the show, then carry on
    if (robot.playReq) {
      robot.playReq = false
      st.show = 0
      st.inspect = -1
      robot.played = []
      st.showFor = nearestMachine(robot.x, robot.z)
    }
    let showing: ReturnType<typeof showStep> = null
    if (st.show >= 0) {
      st.show += dt
      showing = showStep(st.show)
      if (showing && robot.played[robot.played.length - 1] !== showing) robot.played.push(showing)
      robot.playing = showing
      if (!showing) { st.show = -1; robot.playing = null }
    }
    // 1. where to drive: an explicit path (divert / return) first, else the loop
    if (showing) {
      faceYaw = showing === 'scan' ? yawTo(st.showFor) : Math.atan2(camera.position.x - robot.x, camera.position.z - robot.z)
    } else if (st.inspect >= 0) {
      st.inspect += dt / INSPECT_S
      if (st.inspect >= 1) st.inspect = -1
    } else if (st.path.length) {
      moving = drive(st.path[0], dt)
      if (!moving) {
        st.path.shift()
        if (!st.path.length) robot.mode = st.target ? 'parked' : 'patrol'
      }
    } else if (!st.target) {
      const goal = LOOP[st.loopI]
      // a stop on the way: on the same aisle, between here and the corner
      const stopI = STOPS.findIndex((p, i) => !st.stopDone.has(i) && Math.abs(p.z - robot.z) < 0.05 && Math.abs(goal[1] - robot.z) < 0.05 && between(p.x, robot.x, goal[0]))
      const next: [number, number] = stopI >= 0 ? [STOPS[stopI].x, STOPS[stopI].z] : goal
      moving = drive(next, dt)
      if (!moving) {
        if (stopI >= 0) { st.stopDone.add(stopI); st.inspect = 0; st.inspectFor = STOPS[stopI].id }
        else { st.loopI = (st.loopI + 1) % LOOP.length; if (st.loopI === 1) st.stopDone.clear() }
      }
    }
    if (!showing && st.inspect >= 0) faceYaw = yawTo(st.inspectFor)
    if (!showing && robot.mode === 'parked' && st.target) faceYaw = yawTo(st.target)
    function drive([gx, gz]: [number, number], d: number): boolean {
      const dx = gx - robot.x, dz = gz - robot.z, len = Math.hypot(dx, dz)
      if (len < 0.01) { robot.x = gx; robot.z = gz; return false }
      const want = Math.atan2(dx, dz)
      // turn on the spot first if facing far off, then roll
      const off = Math.abs(wrapA(want - st.yaw))
      st.yaw += wrapA(want - st.yaw) * Math.min(1, d * 6)
      if (off > 0.5) return true
      const step = Math.min(len, SPEED * (st.target ? 1.6 : 1) * d)
      robot.x += (dx / len) * step; robot.z += (dz / len) * step
      return true
    }
    if (faceYaw != null) st.yaw += wrapA(faceYaw - st.yaw) * Math.min(1, dt * 4)
    robot.yaw = st.yaw

    // 2. the arm: fold while driving, inspect motion at a stop, point and sweep while parked at an incident
    let goalArm: [number, number] = FOLD
    let light = 0
    if (showing === 'wave') goalArm = WAVE
    else if (showing === 'scan') { goalArm = POINT; light = 1 }
    else if (showing === 'bow') goalArm = BOW
    else if (showing === 'turn') goalArm = RAISE
    else if (st.inspect >= 0) { const p = inspectPose(st.inspect); goalArm = p.a; light = p.light }
    else if (robot.mode === 'parked') { goalArm = POINT; light = 1 }
    const k = st.inspect >= 0 && !showing ? 1 : Math.min(1, dt * (showing ? 7 : 3))
    st.arm[0] += (goalArm[0] - st.arm[0]) * k
    st.arm[1] += (goalArm[1] - st.arm[1]) * k
    st.light += (light - st.light) * Math.min(1, dt * 6)
    const parked = robot.mode === 'parked'
    r.root.position.set(robot.x, moving ? Math.abs(Math.sin(t * 18)) * 0.004 : 0, robot.z)
    r.root.rotation.y = st.yaw
    r.shoulder.rotation.x = st.arm[0]
    r.elbow.rotation.x = st.arm[1]
    r.turret.rotation.y = showing === 'wave' ? Math.sin(t * 7) * 0.5 : parked && !showing ? Math.sin(t * 1.3) * 0.28 : st.inspect >= 0 ? Math.sin(st.inspect * Math.PI * 2) * 0.15 : 0
    r.wrist.rotation.z = showing === 'wave' ? Math.sin(t * 9) * 0.6 : st.light > 0.05 ? Math.sin(t * 2.6) * 0.18 : 0
    // the bow: the whole vehicle dips its nose, twice, like a nod
    st.bow += ((showing === 'bow' ? Math.max(0, Math.sin((st.show - 5.2) * Math.PI * 1.45)) * 0.16 : 0) - st.bow) * Math.min(1, dt * 10)
    r.root.rotation.x = st.bow
    const grip = parked || st.inspect >= 0 ? 0.045 : 0.03
    r.fingerL.position.x = -grip; r.fingerR.position.x = grip
    r.coneMat.opacity = st.light * (0.22 + 0.1 * Math.sin(t * 9))
    r.cone.visible = st.light > 0.02
    r.lensMat.emissiveIntensity = 0.5 + st.light * 2.5
    r.lidar.rotation.y = t * 4
    const alert = !!st.target
    r.beaconMat.emissive.set(alert ? '#f5a623' : '#3fcf5a')
    r.beaconMat.emissiveIntensity = alert ? 0.6 + (Math.sin(t * 8) > 0 ? 1.6 : 0) : 1.1
    r.screenMat.emissive.set(alert ? '#f5a623' : '#3fcf5a')
  })

  // test hook (headless e2e only)
  useEffect(() => {
    if (!import.meta.env.DEV && !new URLSearchParams(location.search).has('test')) return
    const w = window as unknown as Record<string, unknown>
    w.__robot3d = () => ({ ...robot, played: [...robot.played], arm: [...s.current.arm], light: s.current.light, inspecting: s.current.inspect >= 0 })
    return () => { delete w.__robot3d }
  }, [])
  return <primitive object={rig.root} />
}

function nearestMachine(x: number, z: number): string {
  let best = PLACED_IDS[0], bd = Infinity
  for (const id of PLACED_IDS) { const p = PLACED_BY_ID[id]; const d = Math.hypot(p.x - x, p.z - z); if (d < bd) { bd = d; best = id } }
  return best
}
function between(v: number, a: number, b: number) { return (v - a) * (v - b) <= 1e-6 }
function yawTo(id: string): number | null {
  const p = PLACED_BY_ID[id]
  return p ? Math.atan2(p.x - robot.x, p.z - robot.z) : null
}
