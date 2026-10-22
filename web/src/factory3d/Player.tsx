// The lead's controller and camera. First person is the default: eye height scaled to the miniature (the chibi lead is
// ~0.9 tall), WASD walks relative to where you look, Shift runs, mouse look with pointer lock (click the view),
// Q/E or ←/→ turn without the mouse (and drag-to-look when the pointer is not locked), a light head-bob.
// V switches to the overview: the chibi lead (hard hat, vest, phone in hand) followed by an orbit camera at a diorama
// angle, as before (Q/E or drag orbit, wheel zooms). The two cameras blend into each other over about a second.
// Collision comes from the current space (spaces.ts): machines, furniture, walls; walking into a door goes through it.
// Touch (#26): the on-screen stick adds to WASD, one finger drags the view, two fingers pinch-zoom the overview.
import { Html } from '@react-three/drei'
import { useFrame, useThree } from '@react-three/fiber'
import { useEffect, useMemo, useRef } from 'react'
import * as THREE from 'three'
import { HandPhone } from './HandPhone'
import { cyl, makeFigure, mat, mesh, rbox, restPose, walkCycle } from './kit'
import { OVERVIEW_CENTER, nearKey, type Near, type SceneId } from './layout'
import { CAM_LIMITS, OVERVIEW_DIST, TOUCH_DEVICE, cam, director, player, stick, view } from './pose'
import { demoCam } from './demoCamera'
import { walker } from './demoWalk'
import { SPACES } from './spaces'
import { lock, releaseLook, requestLook } from './look'

/** Eye height: just under the top of the chibi lead's head, so machines sit at chest height and the lamps above. */
const EYE = 0.78
const WALK = 1.6
const RUN = 3.2
const TURN = 1.9
const LOOK = 0.0024
const FIG_SCALE = 0.9
const FOV_FIRST = 70
const FOV_THIRD = 32

function typing(t: EventTarget | null) {
  const el = t as HTMLElement | null
  return !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)
}
/** Wrap an angle into (−π, π]. */
const wrap = (a: number) => Math.atan2(Math.sin(a), Math.cos(a))
const ease = (u: number) => u * u * (3 - 2 * u)

function buildLead() {
  const f = makeFigure({ shirt: '#5f8fc4', pants: '#3f4a5a', vest: '#ff8a3d', hair: '#3a2a1e' })
  // hard hat: a yellow dome with a brim
  f.head.add(mesh(new THREE.SphereGeometry(0.15, 12, 6, 0, Math.PI * 2, 0, Math.PI / 2), mat('#f2c14e'), 0, 0.12, 0))
  f.head.add(cyl(0.17, 0.17, 0.02, '#f2c14e', 0, 0.12, 0.02, 14))
  // the supervisor phone in the right hand
  const phone = new THREE.Group()
  phone.position.set(0, -0.03, 0.05)
  phone.add(rbox(0.075, 0.012, 0.13, '#22262c', 0, 0, 0, 0.015))
  const screenMat = new THREE.MeshStandardMaterial({ color: '#000000', emissive: '#3fcf5a', emissiveIntensity: 0.6 })
  const screen = new THREE.Mesh(new THREE.PlaneGeometry(0.06, 0.11), screenMat)
  screen.rotation.x = -Math.PI / 2
  screen.position.y = 0.0135
  phone.add(screen)
  phone.rotation.x = 0.9
  f.handR.add(phone)
  f.root.scale.setScalar(FIG_SCALE)
  f.root.traverse((o) => { if ((o as THREE.Mesh).isMesh) o.castShadow = true })
  // a soft white marker ring at the feet, so the lead is easy to find in the overview
  const ring = new THREE.Mesh(new THREE.RingGeometry(0.2, 0.27, 24), new THREE.MeshBasicMaterial({ color: '#ffffff', transparent: true, opacity: 0.75, depthWrite: false, toneMapped: false }))
  ring.rotation.x = -Math.PI / 2
  ring.position.y = 0.02
  ring.renderOrder = 3
  f.root.add(ring)
  return { f, phone, screenMat }
}

export function Player({ enabled, started, onNear, onDoor, prompt, pending, buzzUntil, phoneUp, mode, scene }: {
  enabled: boolean; started: boolean; onNear: (n: Near) => void; onDoor: (to: SceneId) => void; prompt: string | null
  pending: number; buzzUntil: number; phoneUp: boolean; mode: 'first' | 'third'; scene: SceneId
}) {
  const { camera, gl } = useThree()
  const keys = useRef(new Set<string>())
  const lastNear = useRef<string>('')
  const drag = useRef<{ x: number; y: number; id: number } | null>(null)
  const pinch = useRef<{ pts: Map<number, { x: number; y: number }>; d: number }>({ pts: new Map(), d: 0 })
  const enabledRef = useRef(enabled)
  useEffect(() => { enabledRef.current = enabled }, [enabled])
  const lead = useMemo(() => buildLead(), [])
  const root = useRef<THREE.Group>(null)
  // smoothed orbit state (the targets live in `cam`)
  const orbit = useRef({ yaw: cam.yaw, pitch: cam.pitch, dist: OVERVIEW_DIST, tx: OVERVIEW_CENTER.x, tz: OVERVIEW_CENTER.z })
  /** 0 = orbit camera, 1 = eyes; eases toward the current mode */
  const blend = useRef(0)
  const walkT = useRef(0)
  const bobT = useRef(0)
  const armX = useRef(-0.6)
  const doorArmed = useRef(true)
  const demoWas = useRef(false)
  const tmp = useMemo(() => ({ op: new THREE.Vector3(), oq: new THREE.Quaternion(), ep: new THREE.Vector3(), eq: new THREE.Quaternion(), m: new THREE.Matrix4(), e: new THREE.Euler(0, 0, 0, 'YXZ'), up: new THREE.Vector3(0, 1, 0), tg: new THREE.Vector3() }), [])

  // switching to the overview puts the orbit camera behind the way the lead faces
  useEffect(() => {
    view.mode = mode
    if (mode === 'third') { cam.yaw = orbit.current.yaw + wrap(player.yaw - orbit.current.yaw); releaseLook() }
  }, [mode])
  useEffect(() => {
    if (started && mode === 'third') cam.yaw = orbit.current.yaw + wrap(player.yaw - orbit.current.yaw)
  }, [started, mode])
  // a new space: the lead appears standing still; the door just used is ignored until the lead steps away from it
  useEffect(() => { doorArmed.current = false; keys.current.clear() }, [scene])

  useEffect(() => {
    const el = gl.domElement.parentElement ?? gl.domElement
    lock.el = el
    const down = (e: KeyboardEvent) => {
      if (typing(e.target)) return
      keys.current.add(e.code)
      if (e.code.startsWith('Arrow') || e.code === 'Space') e.preventDefault()
    }
    const up = (e: KeyboardEvent) => keys.current.delete(e.code)
    const blur = () => keys.current.clear()
    const move = (e: PointerEvent) => {
      if (!enabledRef.current) return
      const pc = pinch.current
      if (pc.pts.has(e.pointerId)) pc.pts.set(e.pointerId, { x: e.clientX, y: e.clientY })
      if (pc.pts.size === 2) {   // two fingers: zoom the overview, no look
        const [a, b] = [...pc.pts.values()]
        const d = Math.hypot(a.x - b.x, a.y - b.y)
        if (view.mode === 'third' && pc.d > 0 && d > 0) cam.dist = Math.max(CAM_LIMITS.distMin, Math.min(CAM_LIMITS.distMax, cam.dist * pc.d / d))
        pc.d = d
        return
      }
      if (drag.current && e.pointerId !== drag.current.id) return   // another finger (the stick) moving
      if (view.mode === 'first') {
        if (document.pointerLockElement === el) {
          player.yaw -= e.movementX * LOOK
          player.pitch -= e.movementY * LOOK
        } else if (drag.current) {
          player.yaw -= (e.clientX - drag.current.x) * LOOK * 1.4
          player.pitch -= (e.clientY - drag.current.y) * LOOK * 1.4
          drag.current = { x: e.clientX, y: e.clientY, id: e.pointerId }
        }
        player.pitch = Math.max(-1.25, Math.min(1.25, player.pitch))
      } else if (drag.current) {
        cam.yaw -= (e.clientX - drag.current.x) * 0.006
        cam.pitch = Math.max(CAM_LIMITS.pitchMin, Math.min(CAM_LIMITS.pitchMax, cam.pitch + (e.clientY - drag.current.y) * 0.004))
        drag.current = { x: e.clientX, y: e.clientY, id: e.pointerId }
      }
    }
    const pdown = (e: PointerEvent) => {
      if (e.button !== 0 || !enabledRef.current) return
      const pc = pinch.current
      pc.pts.set(e.pointerId, { x: e.clientX, y: e.clientY })
      if (pc.pts.size === 2) { pc.d = 0; drag.current = null; return }
      drag.current = { x: e.clientX, y: e.clientY, id: e.pointerId }
      requestLook()
    }
    const pup = (e: PointerEvent) => {
      pinch.current.pts.delete(e.pointerId)
      if (drag.current?.id === e.pointerId) drag.current = null
    }
    const wheel = (e: WheelEvent) => {
      if (!enabledRef.current || view.mode !== 'third') return
      e.preventDefault()
      cam.dist = Math.max(CAM_LIMITS.distMin, Math.min(CAM_LIMITS.distMax, cam.dist * Math.exp(e.deltaY * 0.0012)))
    }
    window.addEventListener('keydown', down)
    window.addEventListener('keyup', up)
    window.addEventListener('blur', blur)
    window.addEventListener('pointermove', move)
    el.addEventListener('pointerdown', pdown)
    window.addEventListener('pointerup', pup)
    window.addEventListener('pointercancel', pup)
    el.addEventListener('wheel', wheel, { passive: false })
    return () => {
      window.removeEventListener('keydown', down)
      window.removeEventListener('keyup', up)
      window.removeEventListener('blur', blur)
      window.removeEventListener('pointermove', move)
      el.removeEventListener('pointerdown', pdown)
      window.removeEventListener('pointerup', pup)
      window.removeEventListener('pointercancel', pup)
      el.removeEventListener('wheel', wheel)
      if (lock.el === el) lock.el = null
    }
  }, [gl])

  useEffect(() => { if (!enabled) { keys.current.clear(); drag.current = null; pinch.current.pts.clear(); stick.fw = 0; stick.st = 0 } }, [enabled])

  useFrame(({ clock }, dtRaw) => {
    const dt = Math.min(dtRaw, 0.05)
    const t = clock.elapsedTime
    const k = keys.current
    const { f, phone, screenMat } = lead
    const space = SPACES[scene]
    // the guided demo's first-person walk (demoWalk.ts) moves the lead itself; its opening overview owns the camera
    const demoWalk = director.on && director.mode === 'walk'
    const first = mode === 'first' || demoWalk
    let moved = false
    if (demoWalk) {
      moved = walker.update(Math.min(dtRaw, 0.2))
    } else if (enabled) {
      const turn = (k.has('KeyQ') || k.has('ArrowLeft') ? 1 : 0) - (k.has('KeyE') || k.has('ArrowRight') ? 1 : 0)
      if (first) player.yaw += turn * TURN * dt; else cam.yaw += turn * TURN * dt
      let fw = 0
      let st = 0
      if (k.has('KeyW') || k.has('ArrowUp')) fw += 1
      if (k.has('KeyS') || k.has('ArrowDown')) fw -= 1
      if (k.has('KeyD')) st += 1
      if (k.has('KeyA')) st -= 1
      fw += stick.fw
      st += stick.st
      const len = Math.hypot(fw, st)
      if (len > 0.05) {
        const push = Math.min(1, len)   // the stick walks slower when pushed less; a key is always a full step
        player.running = k.has('ShiftLeft') || k.has('ShiftRight') || Math.hypot(stick.fw, stick.st) > 0.95
        const v = (player.running ? RUN : WALK) * push * dt / len
        const yaw = first ? player.yaw : cam.yaw
        const sin = Math.sin(yaw)
        const cos = Math.cos(yaw)
        // forward on the ground = (−sin yaw, −cos yaw); right = (cos yaw, −sin yaw)
        const dx = (-sin * fw + cos * st) * v
        const dz = (-cos * fw - sin * st) * v
        ;[player.x, player.z] = space.collide(player.x + dx, player.z)
        ;[player.x, player.z] = space.collide(player.x, player.z + dz)
        // overview: turn the figure toward the way it walks
        if (!first) player.yaw += wrap(Math.atan2(-dx, -dz) - player.yaw) * Math.min(1, dt * 12)
        moved = true
      }
    } else if (!started) {
      cam.yaw += dt * 0.12
    }
    player.moving = moved
    if (!moved) player.running = false

    // walking into a door goes through it (after stepping away from the one just used)
    const door = space.touch(player.x, player.z)
    if (!door) doorArmed.current = true
    else if (moved && doorArmed.current && enabled) { doorArmed.current = false; onDoor(door) }

    // figure pose (seen in the overview)
    const g = root.current
    if (g) {
      g.position.set(player.x, 0, player.z)
      f.root.rotation.y = player.yaw + Math.PI
    }
    if (moved) {
      walkT.current += dt * (player.running ? 1.6 : 1)
      walkCycle(f, walkT.current, 10, player.running ? 0.75 : 0.55)
    } else {
      restPose(f)
      f.body.position.y = 0.36 + Math.sin(t * 2) * 0.006   // idle breathing
    }
    const buzzing = performance.now() < buzzUntil
    const armTarget = phoneUp ? -2.3 : moved ? -0.55 + Math.sin(walkT.current * 10) * 0.08 : -0.6
    armX.current += (armTarget - armX.current) * Math.min(1, dt * 14)
    f.armR.rotation.x = armX.current
    f.armR.rotation.z = phoneUp ? -0.5 : 0.08
    f.head.rotation.x = phoneUp ? 0.12 : 0
    phone.rotation.x = phoneUp ? 1.05 : 0.9
    if (phoneUp) { f.armL.rotation.x = -1.2; f.armL.rotation.z = 0.35 }
    phone.position.x = buzzing ? Math.sin(t * 70) * 0.012 : 0
    screenMat.emissive.set(buzzing && Math.sin(t * 20) > 0 ? '#ffffff' : pending > 0 ? '#f5a623' : '#3fcf5a')
    screenMat.emissiveIntensity = pending > 0 ? 0.9 + Math.sin(t * 5) * 0.4 : 0.6

    // the guided demo owns the camera (demoCamera.ts: one continuous take); the lead just stands where it is
    if (director.on && director.mode === 'orbit') {
      demoCam.update(camera as THREE.PerspectiveCamera, t)
      if (g) g.visible = true
      blend.current = 0
      demoWas.current = true
      return
    }
    if (demoWas.current) {
      // the demo's overview ended (the walk begins, or the demo was left): the orbit picks up from where the demo
      // camera is, then blends into the eyes as usual, so this is not a cut either
      demoWas.current = false
      const o = orbit.current
      // walking on: the orbit is set around the lead exactly where the camera is (then the eyes take over);
      // the demo left: the lead stands where the camera was looking
      const lk = demoWalk ? new THREE.Vector3(player.x, 0.55, player.z) : demoCam.lookAt
      const raw = camera.position.distanceTo(lk)
      const d = demoWalk ? raw : Math.max(CAM_LIMITS.distMin, Math.min(CAM_LIMITS.distMax, raw))
      if (!demoWalk) { const [lx, lz] = SPACES[scene].collide(lk.x, lk.z); player.x = lx; player.z = lz; lk.x = lx; lk.z = lz }
      o.tx = lk.x; o.tz = lk.z; o.dist = d
      o.yaw = Math.atan2(camera.position.x - lk.x, camera.position.z - lk.z)
      const pitch = Math.asin(Math.min(1, Math.max(-1, (camera.position.y - 0.55) / Math.max(d, 1e-3))))
      o.pitch = demoWalk ? pitch : Math.max(CAM_LIMITS.pitchMin, Math.min(CAM_LIMITS.pitchMax, pitch))
      cam.yaw = o.yaw; cam.pitch = o.pitch; cam.dist = d
    }
    // orbit camera pose: ease toward the targets (snap after a teleport)
    const vw = orbit.current
    const targetDist = !started ? OVERVIEW_DIST : phoneUp ? Math.min(cam.dist, 8) : cam.dist
    const yawT = cam.yaw + (phoneUp ? 0.85 : 0)
    const tx = started ? player.x : OVERVIEW_CENTER.x, tz = started ? player.z : OVERVIEW_CENTER.z
    if (cam.snap) {
      vw.yaw = yawT; vw.pitch = cam.pitch; vw.dist = targetDist; vw.tx = tx; vw.tz = tz
    } else {
      const e = 1 - Math.exp(-dt * 6)
      vw.yaw += wrap(yawT - vw.yaw) * e
      vw.pitch += (cam.pitch - vw.pitch) * (1 - Math.exp(-dt * 3))
      vw.dist += (targetDist - vw.dist) * (1 - Math.exp(-dt * (started ? 4 : 1)))
      vw.tx += (tx - vw.tx) * (1 - Math.exp(-dt * (started ? 10 : 1.5)))
      vw.tz += (tz - vw.tz) * (1 - Math.exp(-dt * (started ? 10 : 1.5)))
    }
    const ty = 0.55
    const hr = Math.cos(vw.pitch) * vw.dist
    tmp.op.set(vw.tx + Math.sin(vw.yaw) * hr, ty + Math.sin(vw.pitch) * vw.dist, vw.tz + Math.cos(vw.yaw) * hr)
    tmp.m.lookAt(tmp.op, tmp.tg.set(vw.tx, ty, vw.tz), tmp.up)
    tmp.oq.setFromRotationMatrix(tmp.m)

    // eye pose, with a light head-bob while walking
    if (moved) bobT.current += dt * (player.running ? 13 : 9)
    else bobT.current += (Math.round(bobT.current / Math.PI) * Math.PI - bobT.current) * Math.min(1, dt * 8)   // settle
    const bobA = player.running ? 0.016 : 0.01
    tmp.ep.set(player.x + Math.cos(player.yaw) * Math.sin(bobT.current * 0.5) * bobA * 0.6, EYE + Math.abs(Math.sin(bobT.current)) * bobA, player.z - Math.sin(player.yaw) * Math.sin(bobT.current * 0.5) * bobA * 0.6)
    tmp.e.set(player.pitch, player.yaw, 0, 'YXZ')
    tmp.eq.setFromEuler(tmp.e)

    // blend between the two (a teleport snaps it)
    const want = started && first ? 1 : 0
    if (cam.snap) { blend.current = want; cam.snap = false }
    else blend.current += Math.sign(want - blend.current) * Math.min(Math.abs(want - blend.current), Math.min(dtRaw, 0.25) / (director.on ? 0.7 : 1.1))   // real time, even at a low frame rate; the guided demo blends faster
    const b = ease(blend.current)
    camera.position.lerpVectors(tmp.op, tmp.ep, b)
    camera.quaternion.slerpQuaternions(tmp.oq, tmp.eq, b)
    const pc = camera as THREE.PerspectiveCamera
    const fov = FOV_THIRD + (FOV_FIRST - FOV_THIRD) * b
    if (Math.abs(pc.fov - fov) > 0.01) { pc.fov = fov; pc.near = b > 0.5 ? 0.03 : 0.1; pc.updateProjectionMatrix() }
    if (g) g.visible = b < 0.8
    // the demo's no-cut check (e2e) reads the camera's path on this clock during the walk too
    if (demoWalk && demoCam.trace.length < 20000) demoCam.trace.push([t, camera.position.x, camera.position.y, camera.position.z])

    // what is within reach: in first person, a point ahead of the eyes (so it is what you face, not what is beside you)
    const ahead = first ? 0.6 : 0
    const n = space.nearest(player.x - Math.sin(player.yaw) * ahead, player.z - Math.cos(player.yaw) * ahead)
    const key = nearKey(n)
    if (key !== lastNear.current) { lastNear.current = key; onNear(n) }
  })

  return (
    <>
      <group ref={root}>
        <primitive object={lead.f.root} />
        {prompt && mode === 'third' && (
          <Html position={[0, 0, 0]} zIndexRange={[14, 12]} style={{ pointerEvents: 'none' }}>
            <div className="f3-prompt" data-testid="f3-prompt">{!TOUCH_DEVICE && <kbd>F</kbd>} {prompt}</div>
          </Html>
        )}
      </group>
      <HandPhone pending={pending} buzzUntil={buzzUntil} away={phoneUp} visible={started && mode === 'first'} />
    </>
  )
}
