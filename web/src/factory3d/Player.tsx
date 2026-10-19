// First-person controller: WASD + mouse look (pointer lock), Shift to run, Q/E or arrows to turn without a mouse.
// Collision against machine footprints, the desk and the hall walls (layout.collide).
import { useFrame, useThree } from '@react-three/fiber'
import { useEffect, useRef } from 'react'
import { collide, nearest, type Near } from './layout'
import { player } from './pose'

const EYE = 1.7
const WALK = 3.6
const RUN = 7.5
const TURN = 1.9
const LOOK = 0.0022

function typing(t: EventTarget | null) {
  const el = t as HTMLElement | null
  return !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)
}

export function Player({ enabled, onNear }: { enabled: boolean; onNear: (n: Near) => void }) {
  const { camera, gl } = useThree()
  const keys = useRef(new Set<string>())
  const lastNear = useRef<string>('')
  const drag = useRef<{ x: number; y: number } | null>(null)
  const enabledRef = useRef(enabled)
  useEffect(() => { enabledRef.current = enabled }, [enabled])

  useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if (typing(e.target)) return
      keys.current.add(e.code)
      if (e.code.startsWith('Arrow') || e.code === 'Space') e.preventDefault()
    }
    const up = (e: KeyboardEvent) => keys.current.delete(e.code)
    const blur = () => keys.current.clear()
    const move = (e: MouseEvent) => {
      if (!enabledRef.current) return
      if (document.pointerLockElement === gl.domElement) {
        player.yaw -= e.movementX * LOOK
        player.pitch -= e.movementY * LOOK
      } else if (drag.current) {
        // no pointer lock (headless, or the user declined it): drag to look
        player.yaw -= (e.clientX - drag.current.x) * LOOK * 1.5
        player.pitch -= (e.clientY - drag.current.y) * LOOK * 1.5
        drag.current = { x: e.clientX, y: e.clientY }
      }
      player.pitch = Math.max(-1.3, Math.min(1.3, player.pitch))
    }
    const mdown = (e: MouseEvent) => { if (e.button === 0) drag.current = { x: e.clientX, y: e.clientY } }
    const mup = () => { drag.current = null }
    window.addEventListener('keydown', down)
    window.addEventListener('keyup', up)
    window.addEventListener('blur', blur)
    window.addEventListener('mousemove', move)
    gl.domElement.addEventListener('mousedown', mdown)
    window.addEventListener('mouseup', mup)
    return () => {
      window.removeEventListener('keydown', down)
      window.removeEventListener('keyup', up)
      window.removeEventListener('blur', blur)
      window.removeEventListener('mousemove', move)
      gl.domElement.removeEventListener('mousedown', mdown)
      window.removeEventListener('mouseup', mup)
    }
  }, [gl])

  useEffect(() => { if (!enabled) keys.current.clear() }, [enabled])

  useFrame(({ clock }, dtRaw) => {
    const dt = Math.min(dtRaw, 0.05)
    const k = keys.current
    if (enabled) {
      if (k.has('KeyQ') || k.has('ArrowLeft')) player.yaw += TURN * dt
      if (k.has('KeyE') || k.has('ArrowRight')) player.yaw -= TURN * dt
      let f = 0
      let s = 0
      if (k.has('KeyW') || k.has('ArrowUp')) f += 1
      if (k.has('KeyS') || k.has('ArrowDown')) f -= 1
      if (k.has('KeyD')) s += 1
      if (k.has('KeyA')) s -= 1
      const len = Math.hypot(f, s)
      player.moving = len > 0
      if (len > 0) {
        const v = ((k.has('ShiftLeft') || k.has('ShiftRight')) ? RUN : WALK) * dt / len
        const sin = Math.sin(player.yaw)
        const cos = Math.cos(player.yaw)
        // forward = (−sin yaw, −cos yaw); right = (cos yaw, −sin yaw)
        const dx = (-sin * f + cos * s) * v
        const dz = (-cos * f - sin * s) * v
        ;[player.x, player.z] = collide(player.x + dx, player.z)
        ;[player.x, player.z] = collide(player.x, player.z + dz)
      }
    } else player.moving = false
    const bob = player.moving ? Math.sin(clock.elapsedTime * 9) * 0.035 : 0
    camera.position.set(player.x, EYE + bob, player.z)
    camera.rotation.set(player.pitch, player.yaw, 0, 'YXZ')
    const n = nearest(player.x, player.z)
    const key = n ? (n.kind === 'desk' ? 'desk' : n.id) : ''
    if (key !== lastNear.current) { lastNear.current = key; onNear(n) }
  })
  return null
}
