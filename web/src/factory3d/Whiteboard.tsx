// The pitch whiteboard: a mobile whiteboard (frame, legs on castors, marker tray) standing in the lobby by the east
// wall, facing the production floor, showing the current pitch slide as a texture. Clicking it opens the full-screen
// slide view (FactoryApp). The demo's 3:00 cue flies the camera here (WHITEBOARD in layout.ts).
import { useEffect, useMemo } from 'react'
import * as THREE from 'three'
import { box, cyl, mergeStatic, rbox } from './kit'
import { WHITEBOARD } from './layout'

const loader = new THREE.TextureLoader()
const cache = new Map<string, THREE.Texture>()
function texture(url: string): THREE.Texture {
  let t = cache.get(url)
  if (!t) {
    t = loader.load(url)
    t.colorSpace = THREE.SRGBColorSpace
    t.anisotropy = 4
    cache.set(url, t)
  }
  return t
}

export function Whiteboard({ slides, index, onOpen }: { slides: string[]; index: number; onOpen: () => void }) {
  const { w, h, y } = WHITEBOARD
  const frame = useMemo(() => {
    const g = new THREE.Group()
    const F = '#9fb4bf', D = '#5d636b'
    g.add(rbox(w + 0.12, 0.06, 0.06, F, 0, y + h / 2, 0, 0.02))          // top rail
    g.add(rbox(w + 0.12, 0.06, 0.06, F, 0, y - h / 2 - 0.06, 0, 0.02))   // bottom rail
    g.add(box(0.06, h + 0.12, 0.06, F, -(w / 2 + 0.03), y, 0))
    g.add(box(0.06, h + 0.12, 0.06, F, w / 2 + 0.03, y, 0))
    g.add(box(w * 0.6, 0.025, 0.1, F, 0, y - h / 2 - 0.08, 0.06))         // marker tray
    g.add(box(0.12, 0.022, 0.022, '#e07a74', -0.2, y - h / 2 - 0.055, 0.07), box(0.12, 0.022, 0.022, '#6fa8dc', 0.0, y - h / 2 - 0.055, 0.07))
    g.add(box(0.03, h + 0.12, 0.015, '#f6f3ec', 0, y, -0.035))            // back panel
    for (const s of [-1, 1]) {
      const x = s * (w / 2 + 0.03)
      g.add(cyl(0.025, 0.025, y - h / 2, D, x, (y - h / 2) / 2, 0, 8))    // legs
      g.add(box(0.06, 0.04, 0.6, D, x, 0.07, 0))                          // feet
      for (const z of [-0.26, 0.26]) g.add(cyl(0.04, 0.04, 0.04, '#2b2f35', x, 0.04, z, 10).rotateZ(Math.PI / 2))
    }
    return mergeStatic(g, { castShadow: true, receiveShadow: false })
  }, [w, h, y])
  const url = slides[Math.max(0, Math.min(index, slides.length - 1))]
  const map = useMemo(() => (url ? texture(url) : null), [url])
  // preload the neighbours so changing slides is instant
  useEffect(() => { for (const d of [-1, 1]) { const u = slides[index + d]; if (u) texture(u) } }, [slides, index])
  return (
    <group position={[WHITEBOARD.x, 0, WHITEBOARD.z]} rotation={[0, WHITEBOARD.yaw, 0]}>
      <primitive object={frame} />
      <mesh position={[0, y, 0.002]} onClick={(e) => { e.stopPropagation(); onOpen() }}
        onPointerOver={() => { document.body.style.cursor = 'pointer' }} onPointerOut={() => { document.body.style.cursor = '' }}>
        <planeGeometry args={[w, h]} />
        <meshBasicMaterial map={map} color={map ? '#ffffff' : '#fbf8f1'} toneMapped={false} />
      </mesh>
    </group>
  )
}
