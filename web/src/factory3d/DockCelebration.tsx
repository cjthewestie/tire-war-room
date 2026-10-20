// A small celebration at the loading dock when a shipment leaves (issue #36): coloured confetti pops up over the
// dock and falls for ~3 s. `at` changes once per shipment_departed.
import { useFrame } from '@react-three/fiber'
import { useEffect, useMemo, useRef } from 'react'
import * as THREE from 'three'
import { DOCK } from './layout'

const N = 36
const COLORS = ['#76b900', '#ffc94d', '#4fb3d9', '#ff8a50', '#b39ddb']

export function DockCelebration({ at }: { at: number | null }) {
  const group = useRef<THREE.Group>(null)
  const parts = useMemo(() => Array.from({ length: N }, (_, i) => {
    const a = (i / N) * Math.PI * 2
    return { vx: Math.cos(a) * (0.6 + (i % 5) * 0.15), vz: Math.sin(a) * (0.6 + (i % 3) * 0.2), vy: 2.6 + (i % 7) * 0.25, color: COLORS[i % COLORS.length] }
  }), [])
  const start = useRef<number | null>(null)
  useEffect(() => { if (at != null) start.current = -1 }, [at])       // -1: start on the next frame
  useFrame(({ clock }) => {
    const g = group.current
    if (!g) return
    if (start.current === -1) start.current = clock.elapsedTime
    const t = start.current == null ? 99 : clock.elapsedTime - start.current
    g.visible = t < 3.2
    if (!g.visible) return
    g.children.forEach((c, i) => {
      const p = parts[i]
      c.position.set(p.vx * t, Math.max(0.05, p.vy * t - 2.2 * t * t) + 0.3, p.vz * t)
      c.rotation.set(t * 6 + i, t * 4, 0)
    })
  })
  return (
    <group ref={group} position={[(DOCK.x0 + DOCK.x1) / 2, 0, (DOCK.z0 + DOCK.z1) / 2]} visible={false}>
      {parts.map((p, i) => (
        <mesh key={i}>
          <boxGeometry args={[0.07, 0.07, 0.015]} />
          <meshBasicMaterial color={p.color} toneMapped={false} side={THREE.DoubleSide} />
        </mesh>
      ))}
    </group>
  )
}
