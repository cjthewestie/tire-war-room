// Third-person controller: a tiny chibi war-room lead (hard hat, safety vest, phone in hand) followed by an orbit
// camera at a diorama angle. WASD moves relative to the camera, Shift runs, Q/E or mouse drag rotate the camera,
// the wheel zooms. Collision against machine footprints, the hut and the island edge (layout.collide).
import { Html } from '@react-three/drei'
import { useFrame, useThree } from '@react-three/fiber'
import { useEffect, useMemo, useRef } from 'react'
import * as THREE from 'three'
import { cyl, makeFigure, mat, mesh, rbox, restPose, walkCycle } from './kit'
import { collide, nearest, type Near } from './layout'
import { CAM_LIMITS, cam, player } from './pose'

const WALK = 2.2
const RUN = 4.4
const TURN = 1.9
const FIG_SCALE = 0.9

function typing(t: EventTarget | null) {
  const el = t as HTMLElement | null
  return !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)
}
/** Wrap an angle into (−π, π]. */
const wrap = (a: number) => Math.atan2(Math.sin(a), Math.cos(a))

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
  return { f, phone, screenMat }
}

export function Player({ enabled, started, onNear, prompt, pending, buzzUntil, phoneUp }: {
  enabled: boolean; started: boolean; onNear: (n: Near) => void; prompt: string | null
  pending: number; buzzUntil: number; phoneUp: boolean
}) {
  const { camera, gl } = useThree()
  const keys = useRef(new Set<string>())
  const lastNear = useRef<string>('')
  const drag = useRef<{ x: number; y: number } | null>(null)
  const enabledRef = useRef(enabled)
  useEffect(() => { enabledRef.current = enabled }, [enabled])
  const lead = useMemo(() => buildLead(), [])
  const root = useRef<THREE.Group>(null)
  // smoothed camera state (the targets live in `cam`)
  const view = useRef({ yaw: cam.yaw, pitch: cam.pitch, dist: 26, tx: player.x, tz: player.z })
  const walkT = useRef(0)
  const armX = useRef(-0.6)

  // Before the start screen is dismissed the camera floats out over the whole island and slowly circles it.
  useEffect(() => {
    if (started) { cam.yaw = view.current.yaw + wrap(player.yaw - view.current.yaw) }
  }, [started])

  useEffect(() => {
    const el = gl.domElement
    const down = (e: KeyboardEvent) => {
      if (typing(e.target)) return
      keys.current.add(e.code)
      if (e.code.startsWith('Arrow') || e.code === 'Space') e.preventDefault()
    }
    const up = (e: KeyboardEvent) => keys.current.delete(e.code)
    const blur = () => keys.current.clear()
    const move = (e: PointerEvent) => {
      if (!drag.current || !enabledRef.current) return
      cam.yaw -= (e.clientX - drag.current.x) * 0.006
      cam.pitch = Math.max(CAM_LIMITS.pitchMin, Math.min(CAM_LIMITS.pitchMax, cam.pitch + (e.clientY - drag.current.y) * 0.004))
      drag.current = { x: e.clientX, y: e.clientY }
    }
    const pdown = (e: PointerEvent) => { if (e.button === 0) drag.current = { x: e.clientX, y: e.clientY } }
    const pup = () => { drag.current = null }
    const wheel = (e: WheelEvent) => {
      if (!enabledRef.current) return
      e.preventDefault()
      cam.dist = Math.max(CAM_LIMITS.distMin, Math.min(CAM_LIMITS.distMax, cam.dist * Math.exp(e.deltaY * 0.0012)))
    }
    window.addEventListener('keydown', down)
    window.addEventListener('keyup', up)
    window.addEventListener('blur', blur)
    window.addEventListener('pointermove', move)
    el.addEventListener('pointerdown', pdown)
    window.addEventListener('pointerup', pup)
    el.addEventListener('wheel', wheel, { passive: false })
    return () => {
      window.removeEventListener('keydown', down)
      window.removeEventListener('keyup', up)
      window.removeEventListener('blur', blur)
      window.removeEventListener('pointermove', move)
      el.removeEventListener('pointerdown', pdown)
      window.removeEventListener('pointerup', pup)
      el.removeEventListener('wheel', wheel)
    }
  }, [gl])

  useEffect(() => { if (!enabled) keys.current.clear() }, [enabled])

  useFrame(({ clock }, dtRaw) => {
    const dt = Math.min(dtRaw, 0.05)
    const t = clock.elapsedTime
    const k = keys.current
    const { f, phone, screenMat } = lead
    let moved = false
    if (enabled) {
      if (k.has('KeyQ') || k.has('ArrowLeft')) cam.yaw += TURN * dt
      if (k.has('KeyE') || k.has('ArrowRight')) cam.yaw -= TURN * dt
      let fw = 0
      let st = 0
      if (k.has('KeyW') || k.has('ArrowUp')) fw += 1
      if (k.has('KeyS') || k.has('ArrowDown')) fw -= 1
      if (k.has('KeyD')) st += 1
      if (k.has('KeyA')) st -= 1
      const len = Math.hypot(fw, st)
      if (len > 0) {
        player.running = k.has('ShiftLeft') || k.has('ShiftRight')
        const v = (player.running ? RUN : WALK) * dt / len
        const sin = Math.sin(cam.yaw)
        const cos = Math.cos(cam.yaw)
        // camera forward on the ground = (−sin yaw, −cos yaw); right = (cos yaw, −sin yaw)
        const dx = (-sin * fw + cos * st) * v
        const dz = (-cos * fw - sin * st) * v
        ;[player.x, player.z] = collide(player.x + dx, player.z)
        ;[player.x, player.z] = collide(player.x, player.z + dz)
        // turn the figure toward the way it walks
        const want = Math.atan2(-dx, -dz)
        player.yaw += wrap(want - player.yaw) * Math.min(1, dt * 12)
        moved = true
      }
    } else if (!started) {
      cam.yaw += dt * 0.12
    }
    player.moving = moved
    if (!moved) player.running = false

    // figure pose
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
    // phone arm: held low while walking, raised to the face when the phone is up
    const armTarget = phoneUp ? -1.45 : moved ? -0.55 + Math.sin(walkT.current * 10) * 0.08 : -0.6
    armX.current += (armTarget - armX.current) * Math.min(1, dt * 14)
    f.armR.rotation.x = armX.current
    f.armR.rotation.z = phoneUp ? -0.25 : 0.08
    f.head.rotation.x = phoneUp ? 0.28 : 0
    phone.rotation.x = phoneUp ? 0.2 : 0.9
    phone.position.x = buzzing ? Math.sin(t * 70) * 0.012 : 0
    screenMat.emissive.set(buzzing && Math.sin(t * 20) > 0 ? '#ffffff' : pending > 0 ? '#f5a623' : '#3fcf5a')
    screenMat.emissiveIntensity = pending > 0 ? 0.9 + Math.sin(t * 5) * 0.4 : 0.6

    // orbit camera: ease toward the targets (snap after a teleport)
    const vw = view.current
    const targetDist = started ? cam.dist : 26
    if (cam.snap) {
      vw.yaw = cam.yaw; vw.pitch = cam.pitch; vw.dist = targetDist; vw.tx = player.x; vw.tz = player.z
      cam.snap = false
    } else {
      const e = 1 - Math.exp(-dt * 6)
      vw.yaw += wrap(cam.yaw - vw.yaw) * e
      vw.pitch += (cam.pitch - vw.pitch) * e
      vw.dist += (targetDist - vw.dist) * (1 - Math.exp(-dt * (started ? 4 : 1)))
      vw.tx += (player.x - vw.tx) * (1 - Math.exp(-dt * 10))
      vw.tz += (player.z - vw.tz) * (1 - Math.exp(-dt * 10))
    }
    const ty = 0.55
    const hr = Math.cos(vw.pitch) * vw.dist
    camera.position.set(vw.tx + Math.sin(vw.yaw) * hr, ty + Math.sin(vw.pitch) * vw.dist, vw.tz + Math.cos(vw.yaw) * hr)
    camera.lookAt(vw.tx, ty, vw.tz)

    const n = nearest(player.x, player.z)
    const key = n ? (n.kind === 'desk' ? 'desk' : n.id) : ''
    if (key !== lastNear.current) { lastNear.current = key; onNear(n) }
  })

  return (
    <group ref={root}>
      <primitive object={lead.f.root} />
      {prompt && (
        <Html position={[0, 1.25, 0]} center zIndexRange={[8, 6]} style={{ pointerEvents: 'none' }}>
          <div className="f3-prompt" data-testid="f3-prompt"><kbd>F</kbd> {prompt}</div>
        </Html>
      )}
    </group>
  )
}
