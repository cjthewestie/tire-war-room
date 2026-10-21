// First person: the supervisor phone held low in the right hand. It buzzes (shakes, screen flashes) on a new approval,
// glows amber while approvals wait, and drops out of view while the real /phone page is raised (PhoneOverlay).
// Kept small and close to the eye (inside the lead's collision radius), so it never pokes into a wall.
import { useFrame } from '@react-three/fiber'
import { useMemo, useRef } from 'react'
import * as THREE from 'three'
import { LIGHT_HEX } from './layout'

/** A tiny phone-screen layout (status bar, header, three cards), used as the emissive map so the screen colour
 *  (green: all clear, amber: approvals waiting, white flash: buzzing) tints it. */
function screenUi() {
  const c = document.createElement('canvas'); c.width = 64; c.height = 128
  const g = c.getContext('2d')!
  g.fillStyle = '#5a5a5a'; g.fillRect(0, 0, 64, 128)
  g.fillStyle = '#ffffff'; g.fillRect(0, 0, 64, 9)
  g.fillStyle = '#d0d0d0'; g.fillRect(6, 15, 36, 8)
  for (let i = 0; i < 3; i++) { g.fillStyle = i === 0 ? '#ffffff' : '#bdbdbd'; g.beginPath(); g.roundRect(5, 30 + i * 30, 54, 24, 5); g.fill() }
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace
  return t
}

export function HandPhone({ pending, buzzUntil, away, visible }: { pending: number; buzzUntil: number; away: boolean; visible: boolean }) {
  const outer = useRef<THREE.Group>(null)
  const inner = useRef<THREE.Group>(null)
  const screen = useRef<THREE.MeshStandardMaterial>(null)
  const v = useRef(new THREE.Vector3(0.12, -0.3, -0.2))
  const ui = useMemo(() => (typeof document !== 'undefined' ? screenUi() : null), [])
  useFrame(({ camera, clock }) => {
    const g = outer.current
    const i = inner.current
    if (!g || !i) return
    g.visible = visible
    g.position.copy(camera.position)
    g.quaternion.copy(camera.quaternion)
    const buzzing = performance.now() < buzzUntil
    const t = clock.elapsedTime
    // right-hand side of the view, kept on screen on a narrow (portrait) screen too
    const pc = camera as THREE.PerspectiveCamera
    const halfW = 0.2 * Math.tan(((pc.fov ?? 70) * Math.PI) / 360) * (pc.aspect ?? 1.6)
    v.current.set(Math.min(0.105, halfW * 0.62) + (buzzing ? Math.sin(t * 70) * 0.002 : 0), away ? -0.3 : -0.112, -0.2)
    i.position.lerp(v.current, 0.2)
    i.rotation.set(-0.35, -0.35, buzzing ? Math.sin(t * 60) * 0.05 : 0.08)
    if (screen.current) {
      const base = pending > 0 ? LIGHT_HEX.amber : LIGHT_HEX.green
      screen.current.emissive.set(buzzing && Math.sin(t * 20) > 0 ? '#ffffff' : base)
      screen.current.emissiveIntensity = pending > 0 ? 1.2 + Math.sin(t * 5) * 0.3 : 0.9
    }
  }, 0.5)   // after the controller has placed the camera (priority 0), before TiltShift renders (priority 1)
  return (
    <group ref={outer}>
      <group ref={inner} position={[0.105, -0.3, -0.2]} scale={0.21}>
        <mesh><boxGeometry args={[0.09, 0.17, 0.014]} /><meshStandardMaterial color="#22262c" flatShading /></mesh>
        <mesh position={[0, 0, 0.0075]}>
          <planeGeometry args={[0.076, 0.148]} />
          <meshStandardMaterial ref={screen} color="#000000" emissive={LIGHT_HEX.green} emissiveIntensity={0.5} emissiveMap={ui} />
        </mesh>
        {/* yellow case rim, then a low-poly hand: palm + thumb, in the chibi skin tone */}
        <mesh position={[0, 0, -0.009]}><boxGeometry args={[0.098, 0.178, 0.006]} /><meshStandardMaterial color="#f2c14e" flatShading /></mesh>
        <mesh position={[0.01, -0.07, -0.025]} rotation={[0.2, 0, 0]}><boxGeometry args={[0.11, 0.1, 0.04]} /><meshStandardMaterial color="#f1c7a3" flatShading /></mesh>
        <mesh position={[-0.055, -0.03, 0]} rotation={[0, 0, -0.5]}><boxGeometry args={[0.025, 0.07, 0.025]} /><meshStandardMaterial color="#f1c7a3" flatShading /></mesh>
        <mesh position={[0.02, -0.2, -0.04]}><boxGeometry args={[0.1, 0.18, 0.07]} /><meshStandardMaterial color="#5f8fc4" flatShading /></mesh>
      </group>
    </group>
  )
}
