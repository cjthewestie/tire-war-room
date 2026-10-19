// The supervisor phone held low in the right hand while walking. It buzzes (shakes, screen flashes) on a new approval.
import { useFrame } from '@react-three/fiber'
import { useRef } from 'react'
import * as THREE from 'three'

export function HandPhone({ pending, buzzUntil, hidden }: { pending: number; buzzUntil: number; hidden: boolean }) {
  const outer = useRef<THREE.Group>(null)
  const inner = useRef<THREE.Group>(null)
  const screen = useRef<THREE.MeshStandardMaterial>(null)
  const v = useRef(new THREE.Vector3())
  useFrame(({ camera, clock }) => {
    const g = outer.current
    const i = inner.current
    if (!g || !i) return
    g.position.copy(camera.position)
    g.quaternion.copy(camera.quaternion)
    const buzzing = performance.now() < buzzUntil
    const t = clock.elapsedTime
    v.current.set(0.34 + (buzzing ? Math.sin(t * 70) * 0.008 : 0), hidden ? -0.9 : -0.3, -0.62)
    i.position.lerp(v.current, 0.2)
    i.rotation.set(-0.35, -0.35, buzzing ? Math.sin(t * 60) * 0.05 : 0.08)
    if (screen.current) {
      const base = pending > 0 ? '#f2b33d' : '#2e7d32'
      screen.current.emissive.set(buzzing && Math.sin(t * 20) > 0 ? '#ffffff' : base)
      screen.current.emissiveIntensity = pending > 0 ? 0.9 + Math.sin(t * 5) * 0.3 : 0.5
    }
  })
  return (
    <group ref={outer}>
      <group ref={inner} position={[0.34, -0.9, -0.62]}>
        <mesh><boxGeometry args={[0.09, 0.17, 0.012]} /><meshStandardMaterial color="#15171a" flatShading /></mesh>
        <mesh position={[0, 0, 0.0065]}>
          <planeGeometry args={[0.078, 0.15]} />
          <meshStandardMaterial ref={screen} color="#000000" emissive="#2e7d32" emissiveIntensity={0.5} />
        </mesh>
        {/* low-poly hand: palm + thumb */}
        <mesh position={[0.01, -0.07, -0.025]} rotation={[0.2, 0, 0]}><boxGeometry args={[0.11, 0.1, 0.04]} /><meshStandardMaterial color="#c99a78" flatShading /></mesh>
        <mesh position={[-0.055, -0.03, 0]} rotation={[0, 0, -0.5]}><boxGeometry args={[0.025, 0.07, 0.025]} /><meshStandardMaterial color="#c99a78" flatShading /></mesh>
      </group>
    </group>
  )
}
