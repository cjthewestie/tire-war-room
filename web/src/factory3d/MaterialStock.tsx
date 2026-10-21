// Issue #48: the raw materials, visible in the 3D factory. A plate above the loading-area pallet says how the six
// materials stand (GET /api/world materials): 「原料 6 項正常」, or the ones that are low / out with what is left.
// When the patrol (issue #32) has a shortfall open (a material_short plant event), the plate turns red and a ring
// pulses on the floor round the pallet, like a machine in trouble. Read-only.
import { Html } from '@react-three/drei'
import { useFrame } from '@react-three/fiber'
import { useRef } from 'react'
import * as THREE from 'three'
import type { Event, WorldMaterial } from '../contracts'
import { LIGHT_HEX, PALLETS } from './layout'

const [PX, PZ] = PALLETS[1]            // the pallet of crates inside the big doors
const SHOWN = 3
const STATUS_TEXT: Record<WorldMaterial['status'], string> = { ok: '正常', reorder: '已達再訂購點', low: '偏低', out: '缺料' }

export function MaterialStock({ materials, events }: { materials: WorldMaterial[]; events: Event[] }) {
  const ringRef = useRef<THREE.Mesh>(null)
  const short = events.find((e) => (e.anomaly.kind as string) === 'material_short' && e.state !== 'closed' && e.state !== 'safe_stop')
  const off = materials.filter((m) => m.status !== 'ok')
    .sort((a, b) => ['out', 'low', 'reorder'].indexOf(a.status) - ['out', 'low', 'reorder'].indexOf(b.status))
  const level = short || off.some((m) => m.status === 'out') ? 'red' : off.some((m) => m.status === 'low') ? 'amber' : 'green'
  useFrame(({ clock }) => {
    const r = ringRef.current
    if (!r) return
    const u = (clock.elapsedTime * 0.8) % 1
    r.scale.set(1 + u * 0.6, 1 + u * 0.6, 1)
    ;(r.material as THREE.MeshBasicMaterial).opacity = 0.8 * (1 - u)
  })
  return (
    <group position={[PX, 0, PZ]} name="material-stock">
      {short && (
        <mesh ref={ringRef} rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.03, 0]}>
          <ringGeometry args={[0.55, 0.63, 32]} />
          <meshBasicMaterial color={LIGHT_HEX.red} transparent opacity={0.7} depthWrite={false} toneMapped={false} />
        </mesh>
      )}
      <Html position={[0, 1.05, 0]} zIndexRange={[11, 10]} style={{ pointerEvents: 'none' }}>
        <div className={`f3-tag f3-stock ${level}`} data-testid="material-stock" data-level={level}>
          <b>{short ? `缺料風險：${short.anomaly.message.match(/巡檢：(\S+)/)?.[1] ?? '原料'}` : '原料'}</b>
          {off.length === 0
            ? <span>{materials.length} 項正常</span>
            : off.slice(0, SHOWN).map((m) => (
              <i key={m.key}>{m.name} {m.stock.toLocaleString()} {m.unit}・{STATUS_TEXT[m.status]}</i>
            ))}
          {off.length > SHOWN && <i>另 {off.length - SHOWN} 項</i>}
        </div>
      </Html>
    </group>
  )
}
