// Low-poly building blocks, ported from CJ's Minimotive world (minimotive/src/world/kit.ts + islands/common.ts):
// flat-shaded cached materials, box/cyl/ball helpers, the chibi figure rig with walk/rest poses, a gear and a short
// conveyor, plus a helper that merges a static group into one mesh per material.
import * as THREE from 'three'
import { mergeGeometries } from 'three/examples/jsm/utils/BufferGeometryUtils.js'

const mats = new Map<string, THREE.MeshStandardMaterial>()
export function mat(color: string | number, opts: Partial<THREE.MeshStandardMaterialParameters> = {}): THREE.MeshStandardMaterial {
  const key = `${color}|${JSON.stringify(opts)}`
  let m = mats.get(key)
  if (!m) {
    m = new THREE.MeshStandardMaterial({ color, flatShading: true, roughness: 0.82, metalness: 0, ...opts })
    mats.set(key, m)
  }
  return m
}

export function mesh(geo: THREE.BufferGeometry, material: THREE.Material, x = 0, y = 0, z = 0): THREE.Mesh {
  const m = new THREE.Mesh(geo, material)
  m.position.set(x, y, z)
  m.castShadow = true
  m.receiveShadow = true
  return m
}

export const box = (w: number, h: number, d: number, color: string | number, x = 0, y = 0, z = 0) =>
  mesh(new THREE.BoxGeometry(w, h, d), mat(color), x, y, z)

/** A box with softly rounded vertical edges (chunky toy look): an extruded rounded rectangle, base at y=0. */
export function rbox(w: number, h: number, d: number, color: string | number, x = 0, y = 0, z = 0, r = 0.08) {
  // the bevel grows the shape outward by `bs`, so draw the outline that much smaller to keep the true size
  const bev = Math.min(0.04, h / 4)
  const bs = Math.min(bev * 0.8, w / 4, d / 4)
  w -= bs * 2; d -= bs * 2
  const rr = Math.max(0.001, Math.min(r, w / 2 - 0.005, d / 2 - 0.005))
  const s = new THREE.Shape()
  const hw = w / 2 - rr, hd = d / 2 - rr
  s.moveTo(-hw, -d / 2)
  s.lineTo(hw, -d / 2); s.quadraticCurveTo(w / 2, -d / 2, w / 2, -hd)
  s.lineTo(w / 2, hd); s.quadraticCurveTo(w / 2, d / 2, hw, d / 2)
  s.lineTo(-hw, d / 2); s.quadraticCurveTo(-w / 2, d / 2, -w / 2, hd)
  s.lineTo(-w / 2, -hd); s.quadraticCurveTo(-w / 2, -d / 2, -hw, -d / 2)
  const geo = new THREE.ExtrudeGeometry(s, { depth: h - bev * 2, bevelEnabled: true, bevelThickness: bev, bevelSize: bs, bevelSegments: 1, curveSegments: 2 })
  geo.rotateX(-Math.PI / 2)
  geo.translate(0, bev, 0)
  return mesh(geo, mat(color), x, y, z)
}

export const cyl = (rt: number, rb: number, h: number, color: string | number, x = 0, y = 0, z = 0, seg = 10) =>
  mesh(new THREE.CylinderGeometry(rt, rb, h, seg), mat(color), x, y, z)

export const ball = (r: number, color: string | number, x = 0, y = 0, z = 0, detail = 1) =>
  mesh(new THREE.IcosahedronGeometry(r, detail), mat(color), x, y, z)

export type FigureLook = { shirt: string; pants: string; skin?: string; hair?: string; hat?: string; vest?: string; torsoScale?: number }

// The rig faces +z. Limbs hang from pivots, so rotating a pivot swings the limb.
// Negative rotation.x on an arm pivot raises it forward.
export type Figure = {
  root: THREE.Group
  body: THREE.Group
  head: THREE.Group
  armL: THREE.Group
  armR: THREE.Group
  legL: THREE.Group
  legR: THREE.Group
  handR: THREE.Group
  handL: THREE.Group
  torso: THREE.Mesh
}

export function makeFigure(look: FigureLook): Figure {
  const skin = look.skin ?? '#e8b98f'
  const root = new THREE.Group()
  root.userData.figure = true   // FigureFade (the demo keeps people out of the foreground of a framed shot)
  const body = new THREE.Group()
  body.position.y = 0.36
  root.add(body)

  const ts = look.torsoScale ?? 1
  const torso = mesh(new THREE.CylinderGeometry(0.11 * ts, 0.14 * ts, 0.36, 8), mat(look.shirt), 0, 0.18, 0)
  body.add(torso)
  if (look.vest) {
    // safety vest: a slightly wider short shell with a pale reflective band
    body.add(mesh(new THREE.CylinderGeometry(0.118 * ts, 0.148 * ts, 0.24, 8), mat(look.vest), 0, 0.2, 0))
    body.add(mesh(new THREE.CylinderGeometry(0.122 * ts, 0.135 * ts, 0.035, 8), mat('#f6f1e8'), 0, 0.2, 0))
  }

  const head = new THREE.Group()
  head.position.y = 0.5
  body.add(head)
  head.add(ball(0.13, skin, 0, 0.06, 0))
  head.add(mesh(new THREE.SphereGeometry(0.135, 10, 6, 0, Math.PI * 2, 0, Math.PI / 2.2), mat(look.hair ?? '#3a2a1e'), 0, 0.1, -0.03))
  head.add(ball(0.02, '#1c1814', -0.045, 0.04, 0.122, 0))
  head.add(ball(0.02, '#1c1814', 0.045, 0.04, 0.122, 0))
  if (look.hat) {
    head.add(cyl(0.16, 0.16, 0.025, look.hat, 0, 0.15, 0, 12))
    head.add(cyl(0.1, 0.11, 0.1, look.hat, 0, 0.2, 0, 12))
  }

  const arm = (side: number) => {
    const pivot = new THREE.Group()
    pivot.position.set(0.15 * side * ts, 0.33, 0)
    body.add(pivot)
    pivot.add(cyl(0.04, 0.035, 0.3, look.shirt, 0, -0.15, 0, 6))
    const hand = new THREE.Group()
    hand.position.y = -0.32
    hand.add(ball(0.045, skin, 0, 0, 0, 0))
    pivot.add(hand)
    return { pivot, hand }
  }
  const l = arm(-1)
  const r = arm(1)

  const leg = (side: number) => {
    const pivot = new THREE.Group()
    pivot.position.set(0.065 * side, 0.36, 0)
    root.add(pivot)
    pivot.add(cyl(0.05, 0.045, 0.34, look.pants, 0, -0.17, 0, 6))
    pivot.add(box(0.09, 0.05, 0.14, '#2b2420', 0, -0.34, 0.025))
    return pivot
  }

  return { root, body, head, armL: l.pivot, armR: r.pivot, legL: leg(-1), legR: leg(1), handR: r.hand, handL: l.hand, torso }
}

export function walkCycle(f: Figure, t: number, speed = 6, amp = 0.6): void {
  const s = Math.sin(t * speed)
  f.legL.rotation.x = s * amp
  f.legR.rotation.x = -s * amp
  f.armL.rotation.x = -s * amp * 0.8
  f.armR.rotation.x = s * amp * 0.8
  f.body.position.y = 0.36 + Math.abs(Math.cos(t * speed)) * 0.03
}

export function restPose(f: Figure): void {
  for (const p of [f.armL, f.armR, f.legL, f.legR]) p.rotation.set(0, 0, 0)
  f.armL.rotation.z = -0.08
  f.armR.rotation.z = 0.08
  f.body.rotation.set(0, 0, 0)
  f.body.position.y = 0.36
  f.head.rotation.set(0, 0, 0)
}

/** A gear standing upright (spin it with rotation.y of the returned group's child). */
export function gear(r: number, teeth: number, color: string, hub = '#55575a') {
  const outer = new THREE.Group()
  const gr = new THREE.Group()
  gr.add(cyl(r, r, 0.07, color, 0, 0, 0, 14), cyl(r * 0.3, r * 0.3, 0.11, hub, 0, 0, 0, 8))
  for (let i = 0; i < teeth; i++) {
    const a = (i / teeth) * Math.PI * 2
    const tooth = box(r * 0.28, 0.07, r * 0.32, color, Math.cos(a) * (r + r * 0.1), 0, Math.sin(a) * (r + r * 0.1))
    tooth.rotation.y = -a
    gr.add(tooth)
  }
  outer.add(gr)
  outer.rotation.x = Math.PI / 2
  return { group: outer, spin: gr }
}

/** A short conveyor along local x with rollers and boxes sliding along it; returns the group and its animator. */
export function belt(len: number, nBoxes: number, crate = '#c9a06a') {
  const c = new THREE.Group()
  c.add(box(len, 0.06, 0.28, '#5a5a60', 0, 0.26, 0), box(len + 0.04, 0.03, 0.32, '#9aa1a8', 0, 0.21, 0))
  for (const lx of [-len / 2 + 0.08, len / 2 - 0.08]) for (const lz of [-0.11, 0.11]) c.add(box(0.04, 0.2, 0.04, '#9aa1a8', lx, 0.1, lz))
  const boxes = Array.from({ length: nBoxes }, (_, i) => { const b = box(0.13, 0.11, 0.13, i % 3 === 2 ? '#efe6d2' : crate); c.add(b); return b })
  const tick = (t: number, speed: number) => {
    boxes.forEach((b, i) => { const u = (t * speed / len + i / nBoxes) % 1; b.position.set(-len / 2 + 0.08 + u * (len - 0.16), 0.345, 0) })
  }
  tick(0, 0)
  return { group: c, tick }
}

/**
 * Bake a static group into one mesh per material (world transforms applied). Cuts hundreds of draw calls
 * from trees, flowers and machine bodies down to a few. Meshes with differing attribute sets are grouped apart.
 */
export function mergeStatic(src: THREE.Object3D, opts: { castShadow?: boolean; receiveShadow?: boolean } = {}): THREE.Group {
  src.updateMatrixWorld(true)
  const buckets = new Map<string, { m: THREE.Material; geos: THREE.BufferGeometry[]; cast: boolean }>()
  src.traverse((o) => {
    const me = o as THREE.Mesh
    if (!me.isMesh) return
    let geo = me.geometry.clone()
    geo.applyMatrix4(me.matrixWorld)
    if (geo.index) geo = geo.toNonIndexed()
    for (const k of Object.keys(geo.attributes)) if (k !== 'position' && k !== 'normal') geo.deleteAttribute(k)
    const m = me.material as THREE.Material
    const cast = me.castShadow
    const key = `${m.uuid}|${cast}`
    const b = buckets.get(key) ?? { m, geos: [], cast }
    b.geos.push(geo)
    buckets.set(key, b)
  })
  const out = new THREE.Group()
  for (const { m, geos, cast } of buckets.values()) {
    const merged = mergeGeometries(geos, false)
    geos.forEach((g) => g.dispose())
    if (!merged) continue
    const me = new THREE.Mesh(merged, m)
    me.castShadow = opts.castShadow ?? cast
    me.receiveShadow = opts.receiveShadow ?? true
    out.add(me)
  }
  return out
}

/** Small deterministic random, so the scene looks the same on every load. */
export function mulberry(seed: number) {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

/** Roof trusses with pendant lamps over a floor rectangle, seen from inside (first person): cross beams every ~2.2,
 *  two long purlins, and a lamp (cord, shade, warm bulb) between each pair of beams. No roof plate: the open sky
 *  between the beams reads as skylights, and the overview camera looks straight in. */
export function trusses(b: { x0: number; x1: number; z0: number; z1: number }, y: number, wallTop: number, lampsAlongZ: number[]) {
  const g = new THREE.Group()
  const W = '#e7dcc8', D = '#cdbb9c'
  const w = b.x1 - b.x0, d = b.z1 - b.z0
  const n = Math.max(1, Math.round(w / 2.2))
  for (let i = 0; i <= n; i++) {
    const x = b.x0 + (w * i) / n
    g.add(box(0.1, 0.14, d, W, x, y, (b.z0 + b.z1) / 2))
    const ph = y - wallTop
    g.add(box(0.06, ph, 0.06, D, x, wallTop + ph / 2, b.z0 + 0.1), box(0.06, ph, 0.06, D, x, wallTop + ph / 2, b.z1 - 0.1))
  }
  for (const f of [0.33, 0.67]) g.add(box(w, 0.08, 0.08, D, (b.x0 + b.x1) / 2, y + 0.1, b.z0 + d * f))
  for (let i = 0; i < n; i++) {
    const x = b.x0 + (w * (i + 0.5)) / n
    for (const z of lampsAlongZ) {
      g.add(cyl(0.006, 0.006, 0.3, '#5d636b', x, y - 0.15, z, 4))
      g.add(mesh(new THREE.CylinderGeometry(0.05, 0.14, 0.08, 10), mat('#a8d5b5', { emissive: '#a8d5b5', emissiveIntensity: 0.45 }), x, y - 0.34, z))
    }
  }
  g.traverse((o) => { o.castShadow = false; (o as THREE.Mesh).receiveShadow = false })
  return g
}
const BULB = new THREE.MeshStandardMaterial({ color: '#fff3c4', emissive: '#ffe08a', emissiveIntensity: 1.2 })
/** The warm bulbs under the trusses' lamp shades (separate: emissive, not merged with the beams; Roof merges them into one). */
export function bulbs(b: { x0: number; x1: number }, y: number, lampsAlongZ: number[]) {
  const g = new THREE.Group()
  const w = b.x1 - b.x0, n = Math.max(1, Math.round(w / 2.2))
  for (let i = 0; i < n; i++) {
    for (const z of lampsAlongZ) {
      const m = new THREE.Mesh(new THREE.IcosahedronGeometry(0.035, 0), BULB)
      m.position.set(b.x0 + (w * (i + 0.5)) / n, y - 0.39, z)
      g.add(m)
    }
  }
  return g
}
