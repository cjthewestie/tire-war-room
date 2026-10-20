// The miniature factory building (Minimotive look), seen as a dollhouse cutaway: a tiled floor with painted
// walkways, outer walls with windows that drop to a low stub on the camera's side, toy machines grouped by line
// with live status lamps, the 戰情室 (glass walls, desk, and a big screen showing the real dashboard page), the
// 員工待命區, the doors to the 休息區 and 廁所 (separate spaces, Rooms.tsx), and a loading dock outside the big doors.
// Static meshes are merged per material (kit.mergeStatic); only lamps, gears, rollers, belts and people animate.
import { Html } from '@react-three/drei'
import { useFrame } from '@react-three/fiber'
import { useEffect, useMemo, useRef, useState } from 'react'
import * as THREE from 'three'
import { LINE_LABELS, type Line, type MachineTile } from '../contracts'
import { STATUS_LABEL } from '../components/common'
import { LINES } from '../factory'
import type { WarRoomState } from '../store'
import { ball, belt, box, bulbs, cyl, gear, makeFigure, mat, mergeStatic, mesh, rbox, restPose, trusses, walkCycle } from './kit'
import { DockTruck, WorldPeople } from './People'
import { GoodsRings } from './GoodsRings'
import { MachineRings } from './MachineRings'
import { machineRing } from './machineRing'
import {
  BUILDING, DESK, DOCK, DOCK_PROPS, DOOR, DOOR_H, DOOR_W, INNER_WALLS, LAMP_POST, LIGHT_HEX, LINE_HEX, OUTER_WALLS, PALLETS, PLACED, ROOMS, ROOM_X,
  ROW_X0, ROW_Z, SCREEN, STAFF_SPOTS, WALKER_LOOPS, WALL_DOORS, WALL_T, lampPos, statusLight, type Box, type Light, type Placed,
} from './layout'

const CREAM = '#f3ead8', STEEL = '#a9b2ba', DARK = '#5d636b', YELLOW = '#f2c14e', ROLL = '#6b7078', WOOD = '#c89a6a'
const FONT = '"Noto Sans TC", "PingFang TC", "Heiti TC", sans-serif'

/** Mix a hex colour toward white (k = 0..1), for soft pastel machine bodies. */
function soften(hex: string, k: number) {
  return '#' + new THREE.Color(hex).lerp(new THREE.Color('#ffffff'), k).getHexString()
}

type Anim = (t: number) => void
type Toy = { body: THREE.Group; moving: THREE.Group; anim: Anim[] }

/** One chunky toy machine: static body parts (merged later) plus a few moving parts. Base at y=0, centred. */
function buildToy(p: Placed): Toy {
  const body = new THREE.Group()
  const moving = new THREE.Group()
  const anim: Anim[] = []
  const { w, d, h } = p
  const col = soften(LINE_HEX[p.line], 0.25)
  const roller = (len: number, r: number, x: number, y: number, z: number, along: 'x' | 'z' = 'x', speed = 2) => {
    const g = new THREE.Group(); g.position.set(x, y, z)
    const c = cyl(r, r, len, ROLL, 0, 0, 0, 10)
    c.add(box(len * 0.98, r * 0.5, r * 0.25, '#8a9098', 0, 0, r * 0.85))   // a flat so the spin reads
    if (along === 'x') g.rotation.z = Math.PI / 2; else g.rotation.x = Math.PI / 2
    g.add(c); moving.add(g)
    anim.push((t) => { c.rotation.y = t * speed })
  }
  const chimney = (x: number, z: number, hh: number) => {
    body.add(cyl(0.06, 0.07, hh, CREAM, x, hh / 2, z, 8), cyl(0.08, 0.08, 0.05, DARK, x, hh + 0.02, z, 8))
  }
  switch (p.kind) {
    case 'mixer': {
      body.add(rbox(w, h * 0.62, d, col, 0, 0, 0, 0.14))
      body.add(cyl(0.12, w * 0.36, h * 0.3, CREAM, 0, h * 0.62 + h * 0.15, 0, 8))
      chimney(-w * 0.32, -d * 0.3, h * 1.05)
      const gr = gear(0.16, 9, YELLOW); gr.group.position.set(w * 0.18, h * 0.34, d / 2 + 0.04); moving.add(gr.group)
      anim.push((t) => { gr.spin.rotation.y = t * 1.6 })
      break
    }
    case 'mill': case 'bigmill': {
      body.add(rbox(w, h * 0.55, d, col, 0, 0, 0, 0.1))
      for (const s of [-1, 1]) body.add(rbox(0.14, h * 0.95, d * 0.9, YELLOW, s * (w / 2 - 0.07), 0, 0, 0.05))
      roller(w - 0.3, d * 0.17, 0, h * 0.72, -d * 0.2, 'x', 2.2)
      roller(w - 0.3, d * 0.17, 0, h * 0.72, d * 0.2, 'x', -2.2)
      break
    }
    case 'calender': {
      for (const s of [-1, 1]) body.add(rbox(0.16, h, d, col, s * (w / 2 - 0.08), 0, 0, 0.06))
      body.add(rbox(w, 0.12, d, CREAM, 0, 0, 0, 0.05))
      ;[0.32, 0.56, 0.8].forEach((fy, i) => roller(w - 0.34, 0.11, 0, h * fy, 0, 'x', i % 2 ? -2 : 2))
      break
    }
    case 'cooler': {
      const b = belt(w, 5, '#c9a06a'); b.group.scale.set(1, 1.2, d / 0.32); moving.add(b.group)
      anim.push((t) => b.tick(t, 0.25))
      body.add(rbox(w * 0.45, 0.22, d * 0.95, col, 0, 0.36, 0, 0.08))   // cooling hood
      for (const s of [-1, 1]) body.add(cyl(0.06, 0.06, 0.06, CREAM, s * w * 0.12, 0.61, 0, 8))
      break
    }
    case 'winder': case 'payoff': {
      body.add(rbox(w, 0.18, d, col, 0, 0, 0, 0.08))
      for (const s of [-1, 1]) body.add(rbox(0.08, h * 0.65, 0.12, CREAM, s * (w / 2 - 0.1), 0.18, 0, 0.03))
      const sp = new THREE.Group(); sp.position.set(0, h * 0.62, 0)
      const r = Math.min(w, h) * 0.34
      sp.add(cyl(r, r, w * 0.55, p.kind === 'payoff' ? YELLOW : ROLL, 0, 0, 0, 12), cyl(r * 1.15, r * 1.15, 0.04, CREAM, 0, w * 0.28, 0, 12), cyl(r * 1.15, r * 1.15, 0.04, CREAM, 0, -w * 0.28, 0, 12))
      sp.rotation.z = Math.PI / 2
      moving.add(sp)
      anim.push((t) => { sp.rotation.x = t * (p.kind === 'payoff' ? -1.2 : 1.5) })
      break
    }
    case 'hydraulic': {
      body.add(rbox(w, 0.22, d, col, 0, 0, 0, 0.1))
      for (const [sx, sz] of [[-1, -1], [1, -1], [-1, 1], [1, 1]]) body.add(cyl(0.035, 0.035, h, STEEL, sx * w * 0.36, h / 2, sz * d * 0.36, 6))
      body.add(rbox(w, h * 0.22, d, YELLOW, 0, h * 0.78, 0, 0.1))
      const ram = cyl(0.12, 0.12, 0.3, STEEL, 0, 0, 0, 10)
      const plate = rbox(w * 0.6, 0.06, d * 0.6, CREAM, 0, -0.18, 0, 0.05)
      const rg = new THREE.Group(); rg.add(ram, plate); moving.add(rg)
      const ph = (p.x * 1.7) % 6
      anim.push((t) => { rg.position.y = h * 0.62 - (0.5 + 0.5 * Math.sin(t * 1.3 + ph)) * h * 0.18 })
      break
    }
    case 'extruder': {
      body.add(rbox(w, 0.32, d, col, 0, 0, 0, 0.1))
      const barrel = cyl(0.15, 0.15, w * 0.9, STEEL, 0, 0.47, 0, 10); barrel.rotation.z = Math.PI / 2; body.add(barrel)
      body.add(cyl(0.08, 0.22, 0.24, YELLOW, -w * 0.3, 0.72, 0, 8))
      chimney(w * 0.35, -d * 0.25, h * 1.1)
      break
    }
    case 'bladder': {
      body.add(rbox(w, 0.2, d, col, 0, 0, 0, 0.1))
      const bl = ball(w * 0.3, '#5d5a63', 0, h * 0.58, 0, 1)
      moving.add(bl)
      anim.push((t) => { const s = 1 + Math.sin(t * 1.4) * 0.07; bl.scale.set(s, s * 0.92, s) })
      break
    }
    case 'cutter': {
      body.add(rbox(w, h * 0.7, d, col, 0, 0, 0, 0.1))
      body.add(rbox(w * 0.36, 0.14, d * 1.02, YELLOW, 0, h * 0.95, 0, 0.05))
      for (const s of [-1, 1]) body.add(box(0.05, 0.3, 0.05, STEEL, s * w * 0.16, h * 0.82, 0))
      const blade = box(0.03, 0.14, d * 1.05, '#e9eef2', 0, 0, 0)
      moving.add(blade)
      anim.push((t) => { blade.position.y = h * 0.82 - Math.abs(Math.sin(t * 2)) * 0.1 })
      break
    }
    case 'press': {
      body.add(cyl(w * 0.46, w * 0.5, h * 0.45, col, 0, h * 0.225, 0, 10))
      body.add(cyl(w * 0.34, w * 0.44, h * 0.22, STEEL, 0, h * 0.56, 0, 10))
      body.add(cyl(0.05, 0.05, 0.12, DARK, 0, h * 0.73, 0, 6))
      body.add(rbox(w * 0.7, h * 0.85, 0.08, CREAM, 0, 0, -d * 0.44, 0.03))   // back panel
      body.add(box(w * 0.3, 0.12, 0.02, '#4a5560', 0, h * 0.62, -d * 0.39))   // little gauge window
      chimney(w * 0.32, -d * 0.36, h * 1.15)
      break
    }
  }
  return { body, moving, anim }
}

/** Radial-gradient sprite texture for the soft status glow. */
function glowTexture() {
  const c = document.createElement('canvas'); c.width = c.height = 64
  const g = c.getContext('2d')!
  const gr = g.createRadialGradient(32, 32, 0, 32, 32, 32)
  gr.addColorStop(0, 'rgba(255,255,255,1)'); gr.addColorStop(0.35, 'rgba(255,255,255,.55)'); gr.addColorStop(1, 'rgba(255,255,255,0)')
  g.fillStyle = gr; g.fillRect(0, 0, 64, 64)
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace
  return t
}
const GLOW = typeof document !== 'undefined' ? glowTexture() : null

/** Status lamp on a post above the machine, a soft glow sprite behind it, a tinted shell over an abnormal machine,
 *  the event ring, and the reading tag (only for the machine the lead is at; it floats above the lamp). */
function MachineStatus({ p, tile, hot, focus, work }: { p: Placed; tile: MachineTile | undefined; hot: boolean; focus: boolean; work: string | null }) {
  const light: Light = statusLight(tile?.status)
  useEffect(() => { DRAWN_LIGHT[p.id] = light }, [p.id, light])
  const lampRef = useRef<THREE.MeshStandardMaterial>(null)
  const lampMesh = useRef<THREE.Mesh>(null)
  const glowRef = useRef<THREE.Sprite>(null)
  const ringRef = useRef<THREE.Mesh>(null)
  const shellRef = useRef<THREE.MeshBasicMaterial>(null)
  const [lampY, lz] = lampPos(p)
  const alarm = light === 'red' || light === 'amber'
  useFrame(({ clock }) => {
    const t = clock.elapsedTime
    const blink = light === 'red' ? 0.5 + 0.5 * Math.sin(t * 9) : light === 'amber' ? 0.5 + 0.5 * Math.sin(t * 4.5) : 1
    if (lampRef.current) lampRef.current.emissiveIntensity = light === 'grey' ? 0.15 : 0.5 + blink * 1.3
    if (lampMesh.current) lampMesh.current.scale.setScalar(alarm ? 1.5 + blink * 0.25 : 1)
    if (glowRef.current) {
      const s = light === 'grey' ? 0 : light === 'green' ? 0.55 : 0.9 + blink * 0.7
      glowRef.current.scale.set(s, s, 1)
      ;(glowRef.current.material as THREE.SpriteMaterial).opacity = light === 'green' ? 0.45 : 0.45 + blink * 0.5
    }
    if (shellRef.current) shellRef.current.opacity = 0.12 + blink * (light === 'red' ? 0.22 : 0.14)
    if (ringRef.current) {
      const u = (t * 0.8) % 1
      const s = 1 + u * 0.6
      ringRef.current.scale.set(s, s, 1)
      ;(ringRef.current.material as THREE.MeshBasicMaterial).opacity = 0.8 * (1 - u)
    }
  })
  const temp = tile?.temp_c
  const lx = 0
  return (
    <group position={[p.x, 0, p.z]} name={p.id}>
      {alarm && (
        <mesh position={[0, p.h / 2 + 0.02, 0]} renderOrder={1}>
          <boxGeometry args={[p.w + 0.1, p.h + 0.12, p.d + 0.1]} />
          <meshBasicMaterial ref={shellRef} color={LIGHT_HEX[light]} transparent opacity={0.25} depthWrite={false} toneMapped={false} />
        </mesh>
      )}
      <mesh ref={lampMesh} position={[lx, lampY, lz]} castShadow>
        <icosahedronGeometry args={[0.09, 1]} />
        <meshStandardMaterial ref={lampRef} color={LIGHT_HEX[light]} emissive={LIGHT_HEX[light]} flatShading roughness={0.4} />
      </mesh>
      {GLOW && (
        <sprite ref={glowRef} position={[lx, lampY, lz]} renderOrder={2}>
          <spriteMaterial map={GLOW} color={LIGHT_HEX[light]} transparent depthWrite={false} toneMapped={false} />
        </sprite>
      )}
      {hot && (
        <mesh ref={ringRef} rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.03, 0]}>
          <ringGeometry args={[Math.max(p.w, p.d) * 0.7, Math.max(p.w, p.d) * 0.7 + 0.08, 32]} />
          <meshBasicMaterial color={LIGHT_HEX.red} transparent opacity={0.7} depthWrite={false} toneMapped={false} />
        </mesh>
      )}
      {focus && (
        <Html position={[0, lampY + 0.2, lz]} zIndexRange={[11, 10]} style={{ pointerEvents: 'none' }}>
          <div className={`f3-tag ${light}`} data-testid={`tag-${p.id}`}>
            <b>{tile?.short_name ?? p.short}</b>
            <span>{temp == null ? '—' : `${temp.toFixed(1)}°C`}</span>
            <i>{tile ? STATUS_LABEL[tile.status] : '…'}</i>
            {work && <em data-testid={`work-${p.id}`}>{work}</em>}
          </div>
        </Html>
      )}
    </group>
  )
}

/** All 28 toy machines: bodies merged into a handful of meshes, moving parts animated in one loop. */
function Machines() {
  const { statics, moving, anims } = useMemo(() => {
    const all = new THREE.Group()
    const moving = new THREE.Group()
    const anims: Anim[] = []
    for (const p of PLACED) {
      const toy = buildToy(p)
      toy.body.position.set(p.x, 0, p.z)
      all.add(toy.body)
      toy.moving.position.set(p.x, 0, p.z)
      moving.add(toy.moving)
      anims.push(...toy.anim)
      // lamp post (static part of the lamp)
      const [ly, lz] = lampPos(p)
      all.add(cyl(0.025, 0.025, LAMP_POST, DARK, p.x, ly - 0.04 - LAMP_POST / 2, p.z + lz, 5))
    }
    return { statics: mergeStatic(all), moving, anims }
  }, [])
  useFrame(({ clock }) => { const t = clock.elapsedTime; for (const a of anims) a(t) })
  return (<><primitive object={statics} /><primitive object={moving} /></>)
}

/** Canvas texture with Chinese text (no font download: the browser's own CJK font). */
function useTextTexture(w: number, h: number, draw: (g: CanvasRenderingContext2D) => void, key: string) {
  const drawRef = useRef(draw)
  useEffect(() => { drawRef.current = draw })
  const tex = useMemo(() => {
    const c = document.createElement('canvas')
    c.width = w; c.height = h
    const t = new THREE.CanvasTexture(c)
    t.colorSpace = THREE.SRGBColorSpace
    t.anisotropy = 4
    return t
  }, [w, h])
  useEffect(() => {
    const g = (tex.image as HTMLCanvasElement).getContext('2d')
    if (!g) return
    g.clearRect(0, 0, w, h)
    drawRef.current(g)
    tex.needsUpdate = true
  }, [tex, w, h, key])
  useEffect(() => () => tex.dispose(), [tex])
  return tex
}

function roundRect(g: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  g.beginPath(); g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r)
  g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath()
}

/** A little signpost with the line's Chinese name. The board is a DOM chip (like the machine tags), so the
 *  tilt-shift blur never softens it and it stays readable from the far overview. */
/** First person: signs scale with distance (drei distanceFactor) instead of a fixed chip size, so the near ones read
 *  big and the far rows do not clutter the view. */
const SIGN_DF = 6

function LineSign({ line, x, z, fp }: { line: Line; x: number; z: number; fp: boolean }) {
  const post = useMemo(() => {
    const g = new THREE.Group()
    g.add(cyl(0.03, 0.035, 0.95, WOOD, 0, 0.475, 0, 6), cyl(0.09, 0.1, 0.05, LINE_HEX[line], 0, 0.025, 0, 8))
    g.add(rbox(0.5, 0.2, 0.05, '#fffaf0', 0, 0.8, 0, 0.04))
    return g
  }, [line])
  // mount the DOM board one frame late: the very first drei <Html> mounted with the canvas can lose its root
  const [ready, setReady] = useState(false)
  useEffect(() => { const r = requestAnimationFrame(() => setReady(true)); return () => cancelAnimationFrame(r) }, [])
  return (
    <group position={[x, 0, z]}>
      <primitive object={post} />
      {ready && <Html position={[0, 1.0, 0]} center zIndexRange={[9, 5]} distanceFactor={fp ? SIGN_DF : undefined} style={{ pointerEvents: 'none' }}>
        <div className="f3-sign" style={{ borderColor: LINE_HEX[line] }} data-testid={`sign-${line}`}>{LINE_LABELS[line]}</div>
      </Html>}
    </group>
  )
}

/** Floor tile texture: soft cream squares with a faint grout line (one tile per unit, repeated). */
export function tileTexture(base: string, line: string) {
  const c = document.createElement('canvas'); c.width = c.height = 64
  const g = c.getContext('2d')!
  g.fillStyle = base; g.fillRect(0, 0, 64, 64)
  g.strokeStyle = line; g.lineWidth = 2; g.strokeRect(1, 1, 62, 62)
  const t = new THREE.CanvasTexture(c)
  t.colorSpace = THREE.SRGBColorSpace
  t.wrapS = t.wrapT = THREE.RepeatWrapping
  t.anisotropy = 4
  return t
}

const WALL = '#f6efe2', TRIM = '#d9cbb3', ROOF = '#d98f73', GLASS = '#cfe6ee'
const glassMat = () => mat(GLASS, { transparent: true, opacity: 0.38, depthWrite: false, roughness: 0.15 })
const flat = (o: THREE.Mesh) => { o.castShadow = false; return o }

/** One outer-wall side, as a tall wall (sill, window band, pilasters, lintel, roof lip) and a low stub; the tall
 *  version hides whenever the camera is outside that side, so the building reads as a dollhouse cutaway. */
function wallSide(segs: Box[], axis: 'x' | 'z', outward: number) {
  const tall = new THREE.Group(), stub = new THREE.Group()
  const along = (b: Box) => axis === 'x' ? [b.x0, b.x1] : [b.z0, b.z1]
  const at = (b: Box) => axis === 'x' ? (b.z0 + b.z1) / 2 : (b.x0 + b.x1) / 2
  const put = (g: THREE.Group, a0: number, a1: number, c: number, y: number, h: number, t: number, color: string, round = 0.03, m?: THREE.Material) => {
    const len = a1 - a0, mid = (a0 + a1) / 2
    const o = axis === 'x' ? rbox(len, h, t, color, mid, y, c, round) : rbox(t, h, len, color, c, y, mid, round)
    if (m) { o.material = m; o.castShadow = false }
    g.add(o)
  }
  for (const b of segs) {
    const [a0, a1] = along(b), c = at(b)
    // tall: sill, window glass, lintel, pilasters every ~2.2, roof lip leaning out
    put(tall, a0, a1, c, 0, 0.55, WALL_T, WALL)
    put(tall, a0, a1, c, 0.55, 0.8, 0.04, GLASS, 0.01, glassMat())
    put(tall, a0, a1, c, 1.35, 0.4, WALL_T, WALL)
    put(tall, a0, a1, c + outward * 0.12, 1.75, 0.1, 0.42, ROOF, 0.04)
    const n = Math.max(1, Math.round((a1 - a0) / 2.2))
    for (let i = 0; i <= n; i++) {
      const u = a0 + ((a1 - a0) * i) / n
      put(tall, u - 0.11, u + 0.11, c, 0, 1.75, WALL_T + 0.08, TRIM, 0.04)
    }
    // stub: a low wall with a coloured cap, so the floor plan still reads from the open side
    put(stub, a0, a1, c, 0, 0.3, WALL_T, WALL)
    put(stub, a0, a1, c, 0.3, 0.05, WALL_T + 0.04, ROOF, 0.02)
  }
  return { tall: mergeStatic(tall), stub: mergeStatic(stub) }
}

function OuterWalls() {
  const { x0, x1, z0, z1 } = BUILDING
  const sides = useMemo(() => {
    const north = wallSide(OUTER_WALLS.north, 'x', -1)
    const south = wallSide(OUTER_WALLS.south, 'x', 1)
    const west = wallSide(OUTER_WALLS.west, 'z', -1)
    const east = wallSide(OUTER_WALLS.east, 'z', 1)
    // the big doorway: lintel with a rolled-up yellow shutter over the gap (part of the tall south wall)
    const lintel = new THREE.Group()
    lintel.add(rbox(DOOR.x1 - DOOR.x0 + 0.3, 0.4, WALL_T + 0.04, TRIM, (DOOR.x0 + DOOR.x1) / 2, 1.35, z1, 0.03))
    const roll = cyl(0.13, 0.13, DOOR.x1 - DOOR.x0, YELLOW, (DOOR.x0 + DOOR.x1) / 2, 1.22, z1 - 0.12, 10); roll.rotation.z = Math.PI / 2
    lintel.add(roll)
    south.tall.add(mergeStatic(lintel))
    return [
      { ...north, out: (c: THREE.Vector3) => c.z < z0 }, { ...south, out: (c: THREE.Vector3) => c.z > z1 },
      { ...west, out: (c: THREE.Vector3) => c.x < x0 }, { ...east, out: (c: THREE.Vector3) => c.x > x1 },
    ]
  }, [x0, x1, z0, z1])
  useFrame(({ camera }) => {
    for (const s of sides) { const o = s.out(camera.position); s.tall.visible = !o; s.stub.visible = o }
  })
  return <>{sides.map((s, i) => <group key={i}><primitive object={s.tall} /><primitive object={s.stub} /></group>)}</>
}

/** Inner walls: solid low walls with a trim cap, or glass walls (low sill, glass, thin frame and mullions). The wall
 *  behind the big screen is tall so the screen hangs on it. */
function innerWalls() {
  const g = new THREE.Group()
  for (const { box: b, kind } of INNER_WALLS) {
    const w = b.x1 - b.x0, d = b.z1 - b.z0, cx = (b.x0 + b.x1) / 2, cz = (b.z0 + b.z1) / 2
    const alongX = w > d
    if (kind === 'solid') {
      const screenWall = !alongX && Math.abs(cx - ROOMS.staff.x0) < 0.01 && b.z0 < SCREEN.z && b.z1 > SCREEN.z
      const h = screenWall ? 2.0 : 0.85
      g.add(rbox(w, h, d, WALL, cx, 0, cz, 0.03), rbox(w + 0.04, 0.05, d + 0.04, '#a9c9a0', cx, h, cz, 0.02))
    } else {
      g.add(rbox(w, 0.22, d, '#e8e2d6', cx, 0, cz, 0.03))
      g.add(flat(mesh(new THREE.BoxGeometry(alongX ? w : 0.03, 0.9, alongX ? 0.03 : d), glassMat(), cx, 0.67, cz)))
      g.add(box(alongX ? w : 0.06, 0.05, alongX ? 0.06 : d, '#9fb4bf', cx, 1.14, cz))
      const len = alongX ? w : d, n = Math.max(1, Math.round(len / 1.1))
      for (let i = 0; i <= n; i++) {
        const u = (alongX ? b.x0 : b.z0) + (len * i) / n
        g.add(box(0.05, 0.95, 0.05, '#9fb4bf', alongX ? u : cx, 0.69, alongX ? cz : u))
      }
    }
  }
  return g
}

/** Floor, painted walkways, row pads, inner walls and the furniture of every room. All static. */
function Building() {
  const tiles = useMemo(() => {
    const { x0, x1, z0, z1 } = BUILDING
    const w = x1 - x0, d = z1 - z0
    const t = tileTexture('#f4efe5', '#e7dece'); t.repeat.set(w, d)
    return { t, w, d, cx: (x0 + x1) / 2, cz: (z0 + z1) / 2 }
  }, [])
  const statics = useMemo(() => {
    const g = new THREE.Group()
    const { x0, x1, z0, z1 } = BUILDING
    const paint = (w: number, d: number, color: string, x: number, z: number, y = 0.004) => g.add(flat(box(w, 0.006, d, color, x, y, z)))
    // plinth under the floor, and the loading dock slab outside the doors
    g.add(rbox(x1 - x0 + 0.5, 0.32, z1 - z0 + 0.5, '#d6c8b0', (x0 + x1) / 2, -0.32, (z0 + z1) / 2, 0.1))
    g.add(rbox(DOCK.x1 - DOCK.x0, 0.3, DOCK.z1 - DOCK.z0 + 0.2, '#d9d2c4', (DOCK.x0 + DOCK.x1) / 2, -0.3, (DOCK.z0 + DOCK.z1) / 2 + 0.1, 0.05))
    for (let i = 0; i < 9; i++) paint(0.22, 0.14, i % 2 ? '#3d342b' : YELLOW, DOCK.x0 + 0.2 + i * ((DOCK.x1 - DOCK.x0 - 0.4) / 8), DOCK.z1 - 0.1, 0.002)
    // pastel floor pads under every row (D and BOND share one)
    const rows: Line[] = ['A', 'B', 'C', 'D', 'CURE']
    for (const line of rows) {
      const ms = PLACED.filter((p) => p.line === line || (line === 'D' && p.line === 'BOND'))
      const xa = Math.min(...ms.map((p) => p.x - p.w / 2)) - 0.3
      const xb = Math.max(...ms.map((p) => p.x + p.w / 2)) + 0.3
      const dd = Math.max(...ms.map((p) => p.d)) + 0.5
      g.add(flat(rbox(xb - xa, 0.02, dd, soften(LINE_HEX[line], 0.6), (xa + xb) / 2, 0.0, ROW_Z[line], 0.2)))
    }
    // painted walkways: a green path from the war-room door across the yard, a path from the yard to the doors,
    // dashed yellow aisle lines between the rows, and a yellow safety border round the production floor
    paint(5.2, 0.9, '#cfe3c0', 2.2, 0)
    paint(2.4, 3.6, '#cfe3c0', 7.1, 4.4)
    for (const z of [-3.5, -1.3, 0.95, 3.25]) for (let x = -7.2; x < 3.6; x += 0.7) paint(0.4, 0.06, YELLOW, x, z)
    const fx0 = x0 + 0.45, fx1 = ROOM_X - 0.3, fz0 = z0 + 0.35, fz1 = z1 - 0.35
    paint(fx1 - fx0, 0.07, YELLOW, (fx0 + fx1) / 2, fz0); paint(fx1 - fx0, 0.07, YELLOW, (fx0 + fx1) / 2, fz1)
    paint(0.07, fz1 - fz0, YELLOW, fx0, (fz0 + fz1) / 2)
    for (let i = 0; i < 8; i++) paint(0.2, 0.3, i % 2 ? '#3d342b' : YELLOW, DOOR.x0 + 0.2 + i * 0.37, z1 - 0.25)   // hazard band at the doors
    // room floors
    const roomFloor = (r: { x0: number; x1: number; z0: number; z1: number }, c: string) => paint(r.x1 - r.x0 - 0.1, r.z1 - r.z0 - 0.1, c, (r.x0 + r.x1) / 2, (r.z0 + r.z1) / 2, 0.003)
    roomFloor(ROOMS.warRoom, '#e1e9ee'); roomFloor(ROOMS.staff, '#efe3cc')
    for (const d of WALL_DOORS) g.add(doorFrame(d))
    g.add(innerWalls())

    // --- 戰情室: desk with three little monitors facing the lead, the big screen's frame, a plant
    g.add(rbox(DESK.w, DESK.h, DESK.d, WOOD, DESK.x, 0, DESK.z, 0.06))
    g.add(rbox(DESK.w + 0.06, 0.04, DESK.d + 0.06, '#efe6d2', DESK.x, DESK.h, DESK.z, 0.04))
    // three small monitors facing the lead (low, so the big screen behind stays in view from the desk), screens lit
    for (const z of [-0.5, 0, 0.5]) {
      g.add(box(0.03, 0.06, 0.03, DARK, DESK.x + 0.12, DESK.h + 0.05, z))
      g.add(rbox(0.04, 0.15, 0.3, '#3b4450', DESK.x + 0.12, DESK.h + 0.07, z, 0.02))
      g.add(flat(box(0.005, 0.12, 0.26, z === 0 ? '#9fd0e6' : '#b8e0c8', DESK.x + 0.098, DESK.h + 0.145, z)))
    }
    g.add(box(0.12, 0.02, 0.4, '#3b4450', DESK.x - 0.08, DESK.h + 0.05, 0))
    g.add(rbox(0.06, SCREEN.h + 0.16, SCREEN.w + 0.16, '#3b4450', SCREEN.x, SCREEN.y - SCREEN.h / 2 - 0.08, SCREEN.z, 0.05))
    g.add(cyl(0.12, 0.09, 0.2, '#d98a5f', ROOM_X + 0.45, 0.1, ROOMS.warRoom.z1 - 0.45, 8), ball(0.2, '#6f9a4f', ROOM_X + 0.45, 0.36, ROOMS.warRoom.z1 - 0.45, 1))
    g.add(rbox(0.9, 0.04, 0.5, '#b8c9d6', 6.6, 0.002, -1.5, 0.1))   // rug

    // --- 員工待命區: lockers on the east wall, a water cooler
    LOCKERS.forEach((z, i) => g.add(rbox(0.32, 1.0, 0.36, ['#6fa8dc', '#8fbf5a', '#e8c15a', '#a993d6'][i], x1 - 0.3, 0, z, 0.03)))
    g.add(cyl(0.12, 0.12, 0.7, '#fffaf0', 9.95, 0.35, -2.1, 10), cyl(0.1, 0.08, 0.26, '#9fd0e6', 9.95, 0.83, -2.1, 10))

    // --- loading area inside the doors (pallets of crates), the dock outside (crate stack, forklift), a little truck
    const pallet = (x: number, z: number, n: number) => {
      g.add(box(0.7, 0.08, 0.7, '#c89a6a', x, 0.04, z))
      for (let i = 0; i < n; i++) g.add(box(0.3, 0.26, 0.3, i % 3 === 2 ? '#efe6d2' : '#c9a06a', x - 0.16 + (i % 2) * 0.32, 0.21 + Math.floor(i / 4) * 0.26, z - 0.16 + (Math.floor(i / 2) % 2) * 0.32))
    }
    for (const [x, z, n] of PALLETS) pallet(x, z, n)
    const cr = DOCK_PROPS.crates
    for (let i = 0; i < 5; i++) g.add(box(0.32, 0.3, 0.32, i % 3 === 2 ? '#efe6d2' : '#c9a06a', cr.x0 + 0.18 + (i % 2) * 0.34, 0.15 + Math.floor(i / 2) * 0.3, cr.z0 + 0.22 + (Math.floor(i / 2) % 2) * 0.4))
    const fk = DOCK_PROPS.forklift, fx = (fk.x0 + fk.x1) / 2, fz = (fk.z0 + fk.z1) / 2
    g.add(rbox(0.5, 0.35, 0.6, YELLOW, fx, 0.08, fz + 0.1, 0.06), rbox(0.36, 0.06, 0.4, '#3d342b', fx, 0.62, fz + 0.15, 0.03))
    for (const dx of [-0.2, 0.2]) g.add(box(0.04, 0.6, 0.04, '#3d342b', fx + dx, 0.3, fz + 0.32), box(0.04, 0.6, 0.04, '#3d342b', fx + dx, 0.62, fz - 0.05))
    for (const dx of [-0.13, 0.13]) g.add(box(0.06, 0.03, 0.42, '#6b7078', fx + dx, 0.06, fz - 0.33))
    for (const [dx, dz] of [[-0.26, 0.3], [0.26, 0.3], [-0.26, -0.1], [0.26, -0.1]]) { const wh = cyl(0.09, 0.09, 0.06, '#3d342b', fx + dx, 0.09, fz + dz, 10); wh.rotation.z = Math.PI / 2; g.add(wh) }
    // the truck itself is People.tsx DockTruck: it comes and goes with the live world's deliveries and shipments
    return mergeStatic(g)
  }, [])
  return (
    <group>
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[tiles.cx, 0.001, tiles.cz]} receiveShadow>
        <planeGeometry args={[tiles.w, tiles.d]} />
        <meshStandardMaterial map={tiles.t} roughness={0.9} />
      </mesh>
      <primitive object={statics} />
    </group>
  )
}

const LOCKERS = [-1.9, -1.5, -1.1, -0.7]

/** CSS size the dashboard is laid out at on the big screen (16:9, the dashboard's desktop layout). */
const BIG_W = 1600, BIG_H = 900

/** The big screen on the war-room wall shows the REAL dashboard page (/) live: an iframe in a drei <Html transform>
 *  scaled to the screen. "blending" occlusion puts the page behind the canvas and cuts a hole where the screen is
 *  visible, so the desk, the people and the hand phone still draw in front of it. Mounted once, for the whole visit
 *  (hidden, not unmounted, in the other spaces), so it keeps a single /ws connection. Look only: no pointer events;
 *  the desk's computer (F) opens the same page full-size to use it. */
function BigScreen({ active }: { active: boolean }) {
  // mounted a frame late, like the signs (the very first drei <Html> mounted with the canvas can lose its root)
  const [ready, setReady] = useState(false)
  useEffect(() => { const r = requestAnimationFrame(() => setReady(true)); return () => cancelAnimationFrame(r) }, [])
  if (!ready) return null
  return (
    <group position={[SCREEN.x - 0.045, SCREEN.y, SCREEN.z]} rotation={[0, -Math.PI / 2, 0]}>
      <Html transform occlude="blending" distanceFactor={(SCREEN.w / BIG_W) * 400} zIndexRange={[2, 0]} pointerEvents="none"
        geometry={<planeGeometry args={[SCREEN.w, SCREEN.h]} />}>
        <div className="f3-bigscreen" data-testid="f3-bigscreen" style={{ width: BIG_W, height: BIG_H, display: active ? 'block' : 'none' }}>
          <iframe title="戰情室大螢幕（儀表板）" src={`/${location.search}#monitor`} tabIndex={-1} aria-hidden />
        </div>
      </Html>
    </group>
  )
}

/** A door painted on an outer wall: a frame, a pastel leaf with a round window and a knob, a step mat. */
export function doorFrame(d: { x: number; z: number; out: [number, number]; color: string }) {
  const g = new THREE.Group()
  const t = 0.06
  // build facing +z (into the hall) at the origin, then turn to the wall's inward direction
  g.add(rbox(DOOR_W + 0.16, DOOR_H + 0.08, t, TRIM, 0, 0, 0, 0.03))
  g.add(rbox(DOOR_W, DOOR_H, t, d.color, 0, 0, t * 0.6, 0.04))
  g.add(mesh(new THREE.CylinderGeometry(0.13, 0.13, 0.02, 16), mat('#cfe6ee', { roughness: 0.15 }), 0, DOOR_H * 0.72, t * 1.2).rotateX(Math.PI / 2))
  g.add(ball(0.035, YELLOW, DOOR_W * 0.36, DOOR_H * 0.45, t * 1.4, 1))
  g.add(flat(box(DOOR_W + 0.2, 0.01, 0.45, '#c9b89a', 0, 0.005, 0.3)))
  g.position.set(d.x, 0, d.z)
  g.rotation.y = Math.atan2(d.out[0], d.out[1])
  return g
}

/** The 戰情室 board on top of the screen wall, and the beacon. */
function WarRoomScreen() {
  const sign = useTextTexture(512, 192, (g) => {
    roundRect(g, 8, 8, 496, 176, 60); g.fillStyle = '#fffaf0'; g.fill()
    g.lineWidth = 12; g.strokeStyle = '#8fd16a'; g.stroke()
    g.fillStyle = '#3d342b'; g.font = `bold 100px ${FONT}`; g.textAlign = 'center'; g.textBaseline = 'middle'; g.fillText('戰情室', 256, 102)
  }, 'sign')
  const beacon = useMemo(() => {
    const b = ball(0.05, '#ff6b5e', ROOMS.staff.x0, 2.12, -1.0, 0)
    b.material = new THREE.MeshStandardMaterial({ color: '#ff6b5e', emissive: '#ff6b5e', emissiveIntensity: 1 })
    return b
  }, [])
  useFrame(({ clock }) => { (beacon.material as THREE.MeshStandardMaterial).emissiveIntensity = Math.sin(clock.elapsedTime * 3) > 0 ? 1.6 : 0.2 })
  return (
    <group>
      <primitive object={beacon} />
      <group position={[ROOMS.staff.x0, 2.3, 0.2]}>
        <mesh rotation={[0, -Math.PI / 2, 0]}><planeGeometry args={[1.1, 0.41]} /><meshBasicMaterial map={sign} transparent toneMapped={false} /></mesh>
        <mesh rotation={[0, Math.PI / 2, 0]}><planeGeometry args={[1.1, 0.41]} /><meshBasicMaterial map={sign} transparent toneMapped={false} /></mesh>
      </group>
    </group>
  )
}

/** Room name plate: a DOM chip (sharp under the tilt-shift blur), mounted a frame late like the line signs. A fixed
 *  size in first person too: there are few of them, and they are what you look for. */
export function RoomSign({ id, label, x, z, y = 1.3, door }: { id: string; label: string; x: number; z: number; y?: number; door?: string }) {
  const [ready, setReady] = useState(false)
  useEffect(() => { const r = requestAnimationFrame(() => setReady(true)); return () => cancelAnimationFrame(r) }, [])
  return ready ? (
    <Html position={[x, y, z]} center zIndexRange={[9, 5]} style={{ pointerEvents: 'none' }}>
      <div className={`f3-sign f3-room ${door ? 'f3-door-sign' : ''}`} style={door ? { borderColor: door } : undefined} data-testid={`room-${id}`}>{door && <i>🚪</i>}{label}</div>
    </Html>
  ) : null
}

/** The four staff in the 員工待命區: standing idle, breathing, now and then glancing round. */
function StaffCorner() {
  const { group, figs } = useMemo(() => {
    const group = new THREE.Group()
    const looks = [
      { shirt: '#6fa8dc', pants: '#4a5a6a', hat: YELLOW, vest: '#ff8a3d' },
      { shirt: '#8fbf5a', pants: '#5b4a3a', hair: '#7a4a24', vest: '#ff8a3d' },
      { shirt: '#a993d6', pants: '#4a4a5a', hat: '#f6f1e8' },
      { shirt: '#e8c15a', pants: '#3f4a5a', hair: '#2a1e14', vest: '#ff8a3d' },
    ]
    const figs = STAFF_SPOTS.map(([x, z, yaw], i) => {
      const f = makeFigure(looks[i])
      f.root.scale.setScalar(0.85)
      f.root.position.set(x, 0, z)
      f.root.rotation.y = yaw + Math.PI
      restPose(f)
      if (i === 1) { f.armR.rotation.x = -1.0; f.handR.add(cyl(0.035, 0.03, 0.08, '#fffaf0', 0, -0.02, 0.03, 8)) }   // a cup of tea
      group.add(f.root)
      return f
    })
    return { group, figs }
  }, [])
  useFrame(({ clock }) => {
    const t = clock.elapsedTime
    figs.forEach((f, i) => {
      f.body.position.y = 0.36 + Math.sin(t * 2 + i) * 0.007
      f.head.rotation.y = Math.sin(t * 0.4 + i * 1.7) > 0.7 ? 0.5 * Math.sin(t * 0.8 + i) : 0
    })
  })
  return <primitive object={group} />
}

/** Chimney steam over a few curing presses: the small motion that makes a diorama feel alive. */
function Ambient() {
  const { group, puffs } = useMemo(() => {
    const group = new THREE.Group()
    const chimneys = PLACED.filter((p) => p.kind === 'press' && ['CURE-02', 'CURE-06', 'CURE-10'].includes(p.id))
      .map((p) => new THREE.Vector3(p.x + p.w * 0.32, p.h * 1.15 + 0.08, p.z - p.d * 0.36))
    const puffs = Array.from({ length: 12 }, (_, i) => {
      const m = new THREE.Mesh(new THREE.IcosahedronGeometry(0.08, 0), new THREE.MeshStandardMaterial({ color: '#f4efe6', flatShading: true, transparent: true, depthWrite: false }))
      group.add(m)
      return { m, from: chimneys[i % chimneys.length], ph: (i / 12) * 3 }
    })
    return { group, puffs }
  }, [])
  useFrame(({ clock }) => {
    const t = clock.elapsedTime
    for (const p of puffs) {
      const u = ((t + p.ph) % 3) / 3
      p.m.position.set(p.from.x + u * 0.25, p.from.y + u * 0.9, p.from.z - u * 0.1)
      const s = 0.6 + u * 1.6
      p.m.scale.set(s, s, s)
      ;(p.m.material as THREE.MeshStandardMaterial).opacity = 0.85 * (1 - u)
    }
  })
  return <primitive object={group} />
}

/** Background workers walking their loops (Minimotive walker). */
function Walkers({ count = WALKER_LOOPS.length }: { count?: number }) {
  const { group, ticks } = useMemo(() => {
    const group = new THREE.Group()
    const looks = [
      { shirt: '#6fa8dc', pants: '#4a5a6a', hat: YELLOW },
      { shirt: '#e8c15a', pants: '#5b4a3a', hair: '#7a4a24' },
      { shirt: '#a993d6', pants: '#4a4a5a', hat: '#f6f1e8' },
    ]
    const ticks = WALKER_LOOPS.slice(0, count).map((pts, i) => {
      const f = makeFigure(looks[i])
      f.root.scale.setScalar(0.85)
      if (i === 1) f.handR.add(box(0.14, 0.12, 0.14, '#c9a06a', 0, -0.04, 0.07))
      group.add(f.root)
      const segs = pts.map((p, k) => { const q = pts[(k + 1) % pts.length]; return Math.hypot(q[0] - p[0], q[1] - p[1]) })
      const total = segs.reduce((a, b) => a + b, 0)
      let s = (i / count) * total
      const speed = 0.55 + i * 0.08
      return (t: number, dt: number) => {
        s = (s + dt * speed) % total
        let dd = s, k = 0
        while (dd > segs[k]) { dd -= segs[k]; k++ }
        const p = pts[k], q = pts[(k + 1) % pts.length], u = dd / segs[k]
        f.root.position.set(p[0] + (q[0] - p[0]) * u, 0, p[1] + (q[1] - p[1]) * u)
        f.root.rotation.y = Math.atan2(q[0] - p[0], q[1] - p[1])
        walkCycle(f, t, speed * 11, 0.5)
        if (i === 1) f.armR.rotation.x = -0.9
      }
    })
    return { group, ticks }
  }, [count])
  useFrame(({ clock }, dt) => { const t = clock.elapsedTime; for (const tk of ticks) tk(t, Math.min(dt, 0.05)) })
  // test hook (headless e2e only): how many patrol figures walk the aisles
  useEffect(() => {
    if (!import.meta.env.DEV && !new URLSearchParams(location.search).has('test')) return
    const w = window as unknown as Record<string, unknown>
    w.__patrol3d = () => ticks.length
    return () => { delete w.__patrol3d }
  }, [ticks])
  return <primitive object={group} />
}

/** Sun + sky light, shared by every space (the sun's shadow box covers the hall and the small rooms at the origin). */
export function Lights() {
  const sun = useMemo(() => {
    const s = new THREE.DirectionalLight('#ffe9c9', 2.4)
    s.position.set(1.8 + 7, 16, 0.6 + 8)
    s.target.position.set(1.8, 0, 0.6)
    s.castShadow = true
    s.shadow.mapSize.set(2048, 2048)
    Object.assign(s.shadow.camera, { left: -13, right: 13, top: 11, bottom: -11, near: 1, far: 45 })
    s.shadow.bias = -0.0005
    s.shadow.normalBias = 0.03
    s.shadow.radius = 3
    return s
  }, [])
  return (
    <>
      <hemisphereLight args={['#fff6e6', '#8a7458', 1.3]} />
      <primitive object={sun} />
      <primitive object={sun.target} />
    </>
  )
}

/** Where each line's signpost stands: west of its row (BOND in the gap before its press). */
function signSpot(line: Line): [number, number] {
  const ms = PLACED.filter((p) => p.line === line)
  if (line === 'BOND') return [ms[0].x - ms[0].w / 2 - 0.45, ROW_Z[line]]
  return [ROW_X0 - 0.5, ROW_Z[line]]
}

/** Roof trusses and pendant lamps, shown only while the camera is under them (first person); the overview looks in
 *  from above without them. */
export function Roof({ b, y, wallTop, lamps }: { b: Box; y: number; wallTop: number; lamps: number[] }) {
  const g = useMemo(() => {
    const grp = new THREE.Group()
    grp.add(mergeStatic(trusses(b, y, wallTop, lamps), { castShadow: false, receiveShadow: false }), bulbs(b, y, lamps))
    return grp
  }, [b, y, wallTop, lamps])
  useFrame(({ camera }) => { g.visible = camera.position.y < y - 0.2 })
  return <primitive object={g} />
}
const HALL_LAMPS = [-3.5, -1.3, 0.95, 3.25]

/** Live light per machine as last drawn (read by the e2e test hook). */
export const DRAWN_LIGHT: Record<string, Light> = {}

/** The factory hall. `active` false hides it (the lead is in another space) without unmounting the big screen. */
export function World({ state, focusId, active, fp }: { state: WarRoomState; focusId: string | null; active: boolean; fp: boolean }) {
  const tiles = useMemo(() => Object.fromEntries((state.snapshot?.machines ?? []).map((m) => [m.machine_id, m])), [state.snapshot?.machines])
  const hot = useMemo(() => new Set((state.snapshot?.events ?? []).filter((e) => e.state !== 'closed' && e.state !== 'safe_stop').map((e) => e.machine_id)), [state.snapshot?.events])
  const R = ROOMS
  return (
    <group visible={active}>
      {/* soft cream ground round the building (no sea: the building sits on the backdrop) */}
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -0.3, 0]} receiveShadow>
        <circleGeometry args={[160, 48]} />
        <meshStandardMaterial color="#e6dac6" roughness={0.95} />
      </mesh>
      <Building />
      <OuterWalls />
      <Roof b={BUILDING} y={2.5} wallTop={1.85} lamps={HALL_LAMPS} />
      <Machines />
      {PLACED.map((p) => <MachineStatus key={p.id} p={p} tile={tiles[p.id]} hot={hot.has(p.id)} focus={active && focusId === p.id}
        work={active && focusId === p.id ? machineRing(p.id, tiles[p.id], state.world, state.world ? Date.parse(state.world.factory_time) : 0).label : null} />)}
      {active && <>
        {LINES.map((l) => { const [x, z] = signSpot(l); return <LineSign key={l} line={l} x={x} z={z} fp={fp} /> })}
        <RoomSign id="war" label={R.warRoom.label} x={5.6} z={R.warRoom.z0 + 0.3} y={1.45} />
        <RoomSign id="staff" label={R.staff.label} x={(R.staff.x0 + R.staff.x1) / 2} z={R.staff.z1 - 0.3} y={1.5} />
        {WALL_DOORS.map((d) => (
          <RoomSign key={d.to} id={d.to === 'break' ? 'break' : 'restroom'} label={d.label} door={d.color}
            x={d.x + d.out[0] * 0.12} z={d.z + d.out[1] * 0.12} y={DOOR_H + 0.22} />
        ))}
      </>}
      <BigScreen active={active} />
      <WarRoomScreen />
      {/* live world (issue #11): workers go where the sim says, the truck comes and goes; without it, the idle diorama */}
      {/* CJ 2026-10-20 「恢復人力待命 有一兩個人巡邏」: people stand by in the staff corner and two walk a patrol loop;
          a worker leaves standby only for a real task (issue #17) and walks back when it is done */}
      <StaffCorner />
      <Walkers count={state.world ? 2 : WALKER_LOOPS.length} />
      {state.world && <WorldPeople world={state.world} />}
      {state.world?.goods && <GoodsRings goods={state.world.goods} />}
      <MachineRings tiles={tiles} world={state.world} />
      <DockTruck trucks={state.world ? state.world.dock.trucks : null} />
      <Ambient />
    </group>
  )
}
