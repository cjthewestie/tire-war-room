// The miniature factory island (Minimotive look): a squircle island in a pale sea, toy machines grouped by line
// with live status lamps, a war-room hut with the live big screen, trees and flowers on the rim, a few workers.
// Static meshes are merged per material (kit.mergeStatic); only lamps, gears, rollers, belts and people animate.
import { Html } from '@react-three/drei'
import { useFrame } from '@react-three/fiber'
import { useEffect, useMemo, useRef, useState } from 'react'
import * as THREE from 'three'
import { LINE_LABELS, type Event, type Line, type MachineTile } from '../contracts'
import { STATE_LABEL, STATUS_LABEL } from '../components/common'
import { LINES, machineLabel } from '../factory'
import { fmtHM, fmtMD } from '../format'
import type { WarRoomState } from '../store'
import { primaryEvent } from '../store'
import {
  ball, belt, box, cyl, flower, gear, islandBase, makeFigure, mat, mergeStatic, mesh, mulberry, pine, rbox, roundTree, walkCycle,
} from './kit'
import {
  DESK, ISLAND, LIGHT_HEX, LINE_HEX, PLACED, ROW_X0, ROW_Z, SCREEN, WALKER_LOOPS, WAR_ROOM, distToMachine, islandRadius, statusLight,
  type Light, type Placed,
} from './layout'
import { player } from './pose'

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

/** Status lamp on a short post above the machine, a soft glow sprite behind it, the event ring and the near tag. */
function MachineStatus({ p, tile, hot }: { p: Placed; tile: MachineTile | undefined; hot: boolean }) {
  const light: Light = statusLight(tile?.status)
  const lampRef = useRef<THREE.MeshStandardMaterial>(null)
  const glowRef = useRef<THREE.Sprite>(null)
  const ringRef = useRef<THREE.Mesh>(null)
  const [near, setNear] = useState(false)
  const lampY = p.h + 0.32
  useFrame(({ clock }) => {
    const t = clock.elapsedTime
    const blink = light === 'red' ? 0.5 + 0.5 * Math.sin(t * 9) : light === 'amber' ? 0.5 + 0.5 * Math.sin(t * 4.5) : 1
    if (lampRef.current) lampRef.current.emissiveIntensity = light === 'grey' ? 0.15 : 0.5 + blink * 1.3
    if (glowRef.current) {
      const s = light === 'grey' ? 0 : light === 'green' ? 0.55 : 0.6 + blink * 0.5
      glowRef.current.scale.set(s, s, 1)
      ;(glowRef.current.material as THREE.SpriteMaterial).opacity = light === 'green' ? 0.45 : 0.35 + blink * 0.5
    }
    if (ringRef.current) {
      const u = (t * 0.8) % 1
      const s = 1 + u * 0.6
      ringRef.current.scale.set(s, s, 1)
      ;(ringRef.current.material as THREE.MeshBasicMaterial).opacity = 0.8 * (1 - u)
    }
    const n = distToMachine(p, player.x, player.z) < 1.2
    if (n !== near) setNear(n)
  })
  const temp = tile?.temp_c
  const lx = 0, lz = p.line === 'CURE' ? -p.d * 0.15 : 0
  return (
    <group position={[p.x, 0, p.z]} name={p.id}>
      <mesh position={[lx, lampY, lz]} castShadow>
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
      {near && (
        <Html position={[0, lampY + 0.22, lz]} center zIndexRange={[5, 0]} style={{ pointerEvents: 'none' }}>
          <div className={`f3-tag ${light}`} data-testid={`tag-${p.id}`}>
            <b>{tile?.short_name ?? p.short}</b>
            <span>{temp == null ? '—' : `${temp.toFixed(1)}°C`}</span>
            <i>{tile ? STATUS_LABEL[tile.status] : '…'}</i>
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
      const lz = p.line === 'CURE' ? -p.d * 0.15 : 0
      all.add(cyl(0.025, 0.025, 0.3, DARK, p.x, p.h + 0.12, p.z + lz, 5))
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

/** A little signpost with the line's Chinese name; the board always turns to the camera. */
function LineSign({ line, x, z }: { line: Line; x: number; z: number }) {
  const tex = useTextTexture(512, 160, (g) => {
    roundRect(g, 6, 6, 500, 148, 40); g.fillStyle = '#fffaf0'; g.fill()
    g.lineWidth = 10; g.strokeStyle = LINE_HEX[line]; g.stroke()
    g.fillStyle = '#3d342b'; g.font = `bold 66px ${FONT}`; g.textAlign = 'center'; g.textBaseline = 'middle'
    g.fillText(LINE_LABELS[line], 256, 84)
  }, line)
  const post = useMemo(() => {
    const g = new THREE.Group()
    g.add(cyl(0.03, 0.035, 0.95, WOOD, 0, 0.475, 0, 6), cyl(0.09, 0.1, 0.05, LINE_HEX[line], 0, 0.025, 0, 8))
    return g
  }, [line])
  // turn the board about its post toward the camera (yaw only, so it never tips over)
  const board = useRef<THREE.Group>(null)
  useFrame(({ camera }) => { if (board.current) board.current.rotation.y = Math.atan2(camera.position.x - x, camera.position.z - z) })
  return (
    <group position={[x, 0, z]}>
      <primitive object={post} />
      <group ref={board} position={[0, 1.15, 0]}>
        <mesh><planeGeometry args={[1.5, 0.47]} /><meshBasicMaterial map={tex} transparent toneMapped={false} /></mesh>
      </group>
    </group>
  )
}

function ScreenTexture({ state, connected, mode }: { state: WarRoomState; connected: boolean; mode: string }) {
  const snap = state.snapshot
  const ev: Event | null = primaryEvent(snap?.events)
  const ft = state.clock?.factory_time ?? snap?.header.factory_time
  const key = JSON.stringify([snap?.machines.map((m) => m.status + (m.temp_c ?? '')), ev?.id, ev?.state, state.pending.length, ft?.slice(0, 16), connected])
  const tex = useTextTexture(1280, 720, (g) => {
    g.fillStyle = '#1d2733'; g.fillRect(0, 0, 1280, 720)
    g.fillStyle = '#8fd16a'; g.fillRect(0, 0, 1280, 10)
    g.textBaseline = 'top'
    g.fillStyle = '#f3ead8'; g.font = `bold 56px ${FONT}`
    g.fillText('實心胎智慧製造戰情室', 44, 34)
    g.font = `32px ${FONT}`; g.fillStyle = '#b8c2cc'
    g.fillText(`${ft ? `${fmtMD(ft)} ${fmtHM(ft)}` : '—'}  ·  ${connected ? (mode === 'mock' ? '模擬資料' : '即時連線') : '連線中…'}`, 44, 106)
    const h = snap?.header
    g.font = `bold 48px ${FONT}`
    g.fillStyle = '#6fdc7f'; g.fillText(`正常 ${h?.normal_count ?? '—'}`, 44, 170)
    g.fillStyle = (h?.abnormal_count ?? 0) > 0 ? '#ff7b70' : '#b8c2cc'; g.fillText(`異常 ${h?.abnormal_count ?? '—'}`, 320, 170)
    g.fillStyle = state.pending.length ? '#f7c35a' : '#b8c2cc'; g.fillText(`待核准 ${state.pending.length}`, 600, 170)
    g.font = `36px ${FONT}`; g.fillStyle = ev && ev.state !== 'closed' ? '#ff7b70' : '#b8c2cc'
    g.fillText(ev ? `事件：${machineLabel(ev.machine_id)}・${STATE_LABEL[ev.state]}` : '目前無事件', 44, 252)
    if (ev?.alert_card) { g.fillStyle = '#f3ead8'; g.font = `30px ${FONT}`; g.fillText(ev.alert_card.title.slice(0, 38), 44, 302) }
    // mini map: one dot per machine, coloured by status
    const ms = snap?.machines ?? []
    LINES.forEach((line, li) => {
      g.fillStyle = LINE_HEX[line]; g.font = `bold 28px ${FONT}`
      g.fillText(line === 'BOND' ? '貼合' : line === 'CURE' ? '硫化' : line, 44, 372 + li * 56)
      ms.filter((m) => m.line === line).forEach((m, i) => {
        g.fillStyle = LIGHT_HEX[statusLight(m.status)]
        g.beginPath(); g.arc(156 + i * 64, 388 + li * 56, 19, 0, Math.PI * 2); g.fill()
        g.fillStyle = '#1d2733'; g.font = `bold 15px ${FONT}`; g.textAlign = 'center'
        g.fillText(m.short_name.slice(0, 2), 156 + i * 64, 380 + li * 56); g.textAlign = 'left'
      })
    })
  }, key)
  return <meshBasicMaterial map={tex} toneMapped={false} />
}

/** The war-room hut: wooden deck, cream back wall with the live big screen, low side walls, striped awning. */
function WarRoomHut({ state, connected, mode }: { state: WarRoomState; connected: boolean; mode: string }) {
  const { x0, x1, z0, z1 } = WAR_ROOM
  const w = x1 - x0, d = z1 - z0, cx = (x0 + x1) / 2
  const sign = useTextTexture(512, 192, (g) => {
    roundRect(g, 8, 8, 496, 176, 60); g.fillStyle = '#fffaf0'; g.fill()
    g.lineWidth = 12; g.strokeStyle = '#8fd16a'; g.stroke()
    g.fillStyle = '#3d342b'; g.font = `bold 100px ${FONT}`; g.textAlign = 'center'; g.textBaseline = 'middle'; g.fillText('戰情室', 256, 102)
  }, 'sign')
  const { statics, beacon } = useMemo(() => {
    const g = new THREE.Group()
    g.add(rbox(w + 0.2, 0.08, d + 0.2, '#d9b88a', cx, 0, 0, 0.12))                         // deck
    for (let i = 0; i < 7; i++) g.add(box(0.02, 0.005, d + 0.1, '#c7a274', x0 + 0.3 + i * (w / 7), 0.082, 0))
    g.add(rbox(0.22, 2.1, d + 0.2, CREAM, x1, 0, 0, 0.08))                                   // back wall
    g.add(rbox(0.26, 0.12, d + 0.3, '#8fd16a', x1, 2.08, 0, 0.05))                           // green trim
    for (const z of [z0, z1]) {
      g.add(rbox(w - 0.5, 0.62, 0.12, CREAM, cx + 0.25, 0, z, 0.05))                        // low side walls
      g.add(rbox(w - 0.45, 0.06, 0.18, '#8fd16a', cx + 0.25, 0.62, z, 0.03))
      g.add(cyl(0.06, 0.06, 2.1, WOOD, x0 + 0.5, 1.05, z, 8))                                // corner posts
    }
    // short striped canopy over the big screen (the rest of the hut stays open so the camera sees in)
    for (let i = 0; i < 6; i++) {
      const s = box(0.62, 0.05, d / 6 + 0.01, i % 2 ? '#fffaf0' : '#e07a74', x1 - 0.32, 2.12, z0 + d / 12 + (i * d) / 6)
      s.rotation.z = 0.3
      g.add(s)
    }
    g.add(box(0.06, 0.06, d + 0.1, WOOD, x0 + 0.5, 2.12, 0))                                 // front beam
    for (const z of [z0, z1]) g.add(box(w - 0.5, 0.06, 0.06, WOOD, cx + 0.25, 2.12, z))
    // desk + three little monitors facing the lead
    g.add(rbox(DESK.w, DESK.h, DESK.d, WOOD, DESK.x, 0, DESK.z, 0.06))
    g.add(rbox(DESK.w + 0.06, 0.04, DESK.d + 0.06, '#efe6d2', DESK.x, DESK.h, DESK.z, 0.04))
    for (const z of [-0.5, 0, 0.5]) {
      g.add(box(0.03, 0.12, 0.03, DARK, DESK.x + 0.1, DESK.h + 0.1, z))
      const mon = rbox(0.06, 0.24, 0.38, '#3b4450', DESK.x + 0.1, DESK.h + 0.14, z, 0.03)
      g.add(mon)
    }
    g.add(box(0.12, 0.02, 0.4, '#3b4450', DESK.x - 0.08, DESK.h + 0.05, 0))                   // keyboard
    // screen frame on the back wall
    g.add(rbox(0.06, SCREEN.h + 0.16, SCREEN.w + 0.16, '#3b4450', SCREEN.x - 0.12, SCREEN.y - SCREEN.h / 2 - 0.08, SCREEN.z, 0.05))
    // potted plant and a satellite dish on the roof line
    g.add(cyl(0.12, 0.09, 0.2, '#d98a5f', x0 + 0.85, 0.18, z1 - 0.3, 8))
    g.add(ball(0.2, '#6f9a4f', x0 + 0.85, 0.42, z1 - 0.3, 1))
    g.add(cyl(0.02, 0.02, 0.4, STEEL, x1, 2.35, z0 + 0.4, 5))
    const dish = mesh(new THREE.SphereGeometry(0.2, 10, 5, 0, Math.PI * 2, 0, Math.PI / 2.5), mat('#efe6d2', { side: THREE.DoubleSide }), x1 - 0.05, 2.6, z0 + 0.4)
    dish.rotation.z = Math.PI / 2 + 0.5
    g.add(dish)
    g.add(cyl(0.015, 0.015, 0.5, STEEL, x1, 2.4, z1 - 0.3, 4))
    const beacon = ball(0.05, '#ff6b5e', x1, 2.68, z1 - 0.3, 0)
    beacon.material = new THREE.MeshStandardMaterial({ color: '#ff6b5e', emissive: '#ff6b5e', emissiveIntensity: 1 })
    return { statics: mergeStatic(g), beacon }
  }, [w, d, cx, x0, x1, z0, z1])
  useFrame(({ clock }) => { (beacon.material as THREE.MeshStandardMaterial).emissiveIntensity = Math.sin(clock.elapsedTime * 3) > 0 ? 1.6 : 0.2 })
  return (
    <group>
      <primitive object={statics} />
      <primitive object={beacon} />
      <group position={[SCREEN.x - 0.16, SCREEN.y, SCREEN.z]} rotation={[0, -Math.PI / 2, 0]}>
        <mesh><planeGeometry args={[SCREEN.w, SCREEN.h]} /><ScreenTexture state={state} connected={connected} mode={mode} /></mesh>
      </group>
      {/* hut sign standing on the back wall, readable from both sides */}
      <group position={[x1 + 0.05, 2.62, 0]}>
        <mesh rotation={[0, -Math.PI / 2, 0]}><planeGeometry args={[1.3, 0.49]} /><meshBasicMaterial map={sign} transparent toneMapped={false} /></mesh>
        <mesh rotation={[0, Math.PI / 2, 0]}><planeGeometry args={[1.3, 0.49]} /><meshBasicMaterial map={sign} transparent toneMapped={false} /></mesh>
      </group>
    </group>
  )
}

/** Island base, sea-side trees and flowers, soft line pads under each row, crates, lamp posts. All static. */
function Scenery() {
  const statics = useMemo(() => {
    const g = new THREE.Group()
    g.add(islandBase(ISLAND.A, ISLAND.B, ISLAND.ground, ISLAND.seed))
    const rand = mulberry(7)
    // pastel floor pads under every row (D and BOND share one)
    const rows: Line[] = ['A', 'B', 'C', 'D', 'CURE']
    for (const line of rows) {
      const ms = PLACED.filter((p) => p.line === line || (line === 'D' && p.line === 'BOND'))
      const xa = Math.min(...ms.map((p) => p.x - p.w / 2)) - 0.3
      const xb = Math.max(...ms.map((p) => p.x + p.w / 2)) + 0.3
      const dd = Math.max(...ms.map((p) => p.d)) + 0.5
      const pad = rbox(xb - xa, 0.03, dd, soften(LINE_HEX[line], 0.6), (xa + xb) / 2, 0.0, ROW_Z[line], 0.2)
      pad.castShadow = false
      g.add(pad)
    }
    // a sandy path from the yard to the hut
    const path = rbox(1.3, 0.025, 1.2, '#e8d8b0', 4.3, 0, 0, 0.3); path.castShadow = false; g.add(path)
    // trees and flowers around the rim, kept out of the works and the hut
    const busy = (x: number, z: number) =>
      (x > ROW_X0 - 1.1 && x < 4.6 && z > -5.6 && z < 5.3) || (x > WAR_ROOM.x0 - 0.6 && x < WAR_ROOM.x1 + 0.7 && z > WAR_ROOM.z0 - 0.7 && z < WAR_ROOM.z1 + 0.7)
    const greens = ['#4f7a43', '#5d8a4c', '#6f9a4f', '#7aa35a']
    for (let i = 0; i < 46; i++) {
      const a = (i / 46) * Math.PI * 2 + rand() * 0.08
      const r = islandRadius(a) - 0.55 - rand() * 0.9
      const x = Math.cos(a) * r, z = Math.sin(a) * r
      if (busy(x, z)) continue
      const s = 0.65 + rand() * 0.45
      if (rand() < 0.55) pine(g, x, z, s, greens[i % 2]); else roundTree(g, x, z, s, greens[2 + (i % 2)])
    }
    const petals = ['#e2b34a', '#d9534f', '#f6f1e8', '#b98ad0', '#e88fb0']
    for (let i = 0; i < 150; i++) {
      const a = rand() * Math.PI * 2
      const r = islandRadius(a) - 0.4 - rand() * 2.2
      const x = Math.cos(a) * r, z = Math.sin(a) * r
      if (busy(x, z)) continue
      flower(g, x, z, petals[i % petals.length])
    }
    // little rocks on the beach
    for (let i = 0; i < 14; i++) {
      const a = rand() * Math.PI * 2, r = islandRadius(a) + 0.25
      const rock = ball(0.1 + rand() * 0.12, '#b9b2a4', Math.cos(a) * r, -0.18, Math.sin(a) * r, 0)
      g.add(rock)
    }
    // crate stack and barrels in the yard, a lamp post by the path
    for (let i = 0; i < 5; i++) g.add(box(0.26, 0.26, 0.26, i % 3 === 2 ? '#efe6d2' : '#c9a06a', 1.3 + (i % 3) * 0.28, 0.13 + Math.floor(i / 3) * 0.26, -1.1))
    for (let i = 0; i < 3; i++) g.add(cyl(0.1, 0.1, 0.28, i === 1 ? '#e07a74' : '#6fa8dc', 2.4 + i * 0.23, 0.14, -1.25, 10))
    g.add(cyl(0.025, 0.03, 1.1, '#5b4636', 4.3, 0.55, -0.85, 6), ball(0.07, '#fff3cf', 4.3, 1.12, -0.85, 0))
    // a picnic bench by the trees, for cuteness
    g.add(box(0.7, 0.05, 0.22, WOOD, 2.6, 0.24, -6.3), box(0.05, 0.24, 0.2, WOOD, 2.3, 0.12, -6.3), box(0.05, 0.24, 0.2, WOOD, 2.9, 0.12, -6.3))
    return mergeStatic(g)
  }, [])
  return <primitive object={statics} />
}

/** Floating boats and the chimney steam: the small motion that makes a diorama feel alive. */
function Ambient() {
  const { group, boats, puffs } = useMemo(() => {
    const group = new THREE.Group()
    const boats = [0, 1].map((i) => {
      const b = new THREE.Group()
      b.add(box(0.9, 0.25, 0.35, ['#e07a74', '#6fa8dc'][i], 0, -0.75, 0))
      const sail = mesh(new THREE.ConeGeometry(0.35, 0.9, 3), mat('#f6f1e8'), 0, -0.15, 0)
      sail.scale.z = 0.15
      b.add(sail, cyl(0.02, 0.02, 1, '#6e4526', 0, -0.2, 0, 4))
      group.add(b)
      return b
    })
    const chimneys = PLACED.filter((p) => p.kind === 'press' && ['CURE-02', 'CURE-06', 'CURE-10'].includes(p.id))
      .map((p) => new THREE.Vector3(p.x + p.w * 0.32, p.h * 1.15 + 0.08, p.z - p.d * 0.36))
    const puffs = Array.from({ length: 12 }, (_, i) => {
      const m = new THREE.Mesh(new THREE.IcosahedronGeometry(0.08, 0), new THREE.MeshStandardMaterial({ color: '#f4efe6', flatShading: true, transparent: true, depthWrite: false }))
      group.add(m)
      return { m, from: chimneys[i % chimneys.length], ph: (i / 12) * 3 }
    })
    return { group, boats, puffs }
  }, [])
  useFrame(({ clock }) => {
    const t = clock.elapsedTime
    boats.forEach((b, i) => {
      const a = t * 0.05 + i * Math.PI
      b.position.set(Math.cos(a) * 15, Math.sin(t * 1.3 + i) * 0.04, Math.sin(a) * 11.5)
      b.rotation.y = -a
    })
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
function Walkers() {
  const { group, ticks } = useMemo(() => {
    const group = new THREE.Group()
    const looks = [
      { shirt: '#6fa8dc', pants: '#4a5a6a', hat: YELLOW },
      { shirt: '#e8c15a', pants: '#5b4a3a', hair: '#7a4a24' },
      { shirt: '#a993d6', pants: '#4a4a5a', hat: '#f6f1e8' },
    ]
    const ticks = WALKER_LOOPS.map((pts, i) => {
      const f = makeFigure(looks[i])
      f.root.scale.setScalar(0.85)
      if (i === 1) f.handR.add(box(0.14, 0.12, 0.14, '#c9a06a', 0, -0.04, 0.07))
      group.add(f.root)
      const segs = pts.map((p, k) => { const q = pts[(k + 1) % pts.length]; return Math.hypot(q[0] - p[0], q[1] - p[1]) })
      const total = segs.reduce((a, b) => a + b, 0)
      let s = (i / 3) * total
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
  }, [])
  useFrame(({ clock }, dt) => { const t = clock.elapsedTime; for (const tk of ticks) tk(t, Math.min(dt, 0.05)) })
  return <primitive object={group} />
}

function Lights() {
  const sun = useMemo(() => {
    const s = new THREE.DirectionalLight('#ffe9c9', 2.4)
    s.position.set(9, 16, 7)
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
    </>
  )
}

/** Where each line's signpost stands: west of its row (BOND in the gap before its press). */
function signSpot(line: Line): [number, number] {
  const ms = PLACED.filter((p) => p.line === line)
  if (line === 'BOND') return [ms[0].x - ms[0].w / 2 - 0.45, ROW_Z[line]]
  return [ROW_X0 - 0.5, ROW_Z[line]]
}

export function World({ state, connected, mode }: { state: WarRoomState; connected: boolean; mode: string }) {
  const tiles = useMemo(() => Object.fromEntries((state.snapshot?.machines ?? []).map((m) => [m.machine_id, m])), [state.snapshot?.machines])
  const hot = useMemo(() => new Set((state.snapshot?.events ?? []).filter((e) => e.state !== 'closed' && e.state !== 'safe_stop').map((e) => e.machine_id)), [state.snapshot?.events])
  return (
    <group>
      <Lights />
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -0.9, 0]} receiveShadow>
        <circleGeometry args={[160, 64]} />
        <meshStandardMaterial color="#a9c9c6" roughness={0.35} />
      </mesh>
      <Scenery />
      <Machines />
      {PLACED.map((p) => <MachineStatus key={p.id} p={p} tile={tiles[p.id]} hot={hot.has(p.id)} />)}
      {LINES.map((l) => { const [x, z] = signSpot(l); return <LineSign key={l} line={l} x={x} z={z} /> })}
      <WarRoomHut state={state} connected={connected} mode={mode} />
      <Ambient />
      <Walkers />
    </group>
  )
}
