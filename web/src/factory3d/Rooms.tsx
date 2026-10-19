// The two spaces outside the factory hall, reached through the doors on its walls (CJ, 2026-10-19:
// 「休息區跟廁所是要真的傳送到另一個空間」): the 休息區 (a small yard room with a half court) and the 廁所. Same
// toy look as the hall: flat-shaded pastel blocks, merged per material. Each has an exit door back to the hall.
// Floor plans and colliders live in spaces.ts.
import { useFrame } from '@react-three/fiber'
import { useMemo } from 'react'
import * as THREE from 'three'
import { ball, box, cyl, makeFigure, mat, mergeStatic, mesh, rbox, restPose } from './kit'
import type { Box } from './layout'
import { BREAK, BREAK_ROOM, TOILET, TOILET_ROOM, type Room } from './spaces'
import { Roof, RoomSign, doorFrame, tileTexture } from './World'

const WALL = '#f6efe2', CAP = '#d98f73', WOOD = '#c89a6a', YELLOW = '#f2c14e', GLASS = '#cfe6ee'
const WALL_H = 1.75
const ROOM_LAMPS = [0]
const flat = (o: THREE.Mesh) => { o.castShadow = false; return o }

/** Soft sky with a few clouds, for the view out of the windows. */
function skyTexture() {
  const c = document.createElement('canvas'); c.width = 256; c.height = 128
  const g = c.getContext('2d')!
  const gr = g.createLinearGradient(0, 0, 0, 128)
  gr.addColorStop(0, '#9fd3ef'); gr.addColorStop(1, '#e4f3f8')
  g.fillStyle = gr; g.fillRect(0, 0, 256, 128)
  g.fillStyle = 'rgba(255,255,255,.9)'
  for (const [x, y, r] of [[40, 30, 14], [58, 26, 18], [76, 32, 12], [170, 50, 12], [186, 44, 16], [204, 52, 11]]) { g.beginPath(); g.arc(x, y, r, 0, Math.PI * 2); g.fill() }
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace
  return t
}

/** Walls of a room: solid with a coloured cap, or (on `windows` sides) a sill, a glass band and a lintel, with a sky
 *  backdrop and little trees outside. The south wall keeps a gap for the exit door, drawn as a door leaf. */
function roomShell(r: Room, windows: ('north' | 'west' | 'east')[], floorColor: string, grout: string, color: string) {
  const g = new THREE.Group()
  const { x0, x1, z0, z1 } = r.floor
  const w = x1 - x0, d = z1 - z0
  // plinth, floor tiles
  g.add(rbox(w + 0.5, 0.3, d + 0.5, '#d6c8b0', (x0 + x1) / 2, -0.3, (z0 + z1) / 2, 0.1))
  const tex = tileTexture(floorColor, grout); tex.repeat.set(w * 2, d * 2)
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(w, d), new THREE.MeshStandardMaterial({ map: tex, roughness: 0.9 }))
  floor.rotation.x = -Math.PI / 2; floor.position.set((x0 + x1) / 2, 0.001, (z0 + z1) / 2); floor.receiveShadow = true
  const side = (b: Box): 'north' | 'south' | 'west' | 'east' => {
    if (b.z1 - b.z0 < b.x1 - b.x0) return Math.abs((b.z0 + b.z1) / 2 - z0) < 0.01 ? 'north' : 'south'
    return Math.abs((b.x0 + b.x1) / 2 - x0) < 0.01 ? 'west' : 'east'
  }
  for (const b of r.walls) {
    const bw = b.x1 - b.x0, bd = b.z1 - b.z0, cx = (b.x0 + b.x1) / 2, cz = (b.z0 + b.z1) / 2
    const s = side(b)
    if ((windows as string[]).includes(s)) {
      g.add(rbox(bw, 0.55, bd, WALL, cx, 0, cz, 0.03), rbox(bw, 0.4, bd, WALL, cx, 1.35, cz, 0.03))
      g.add(flat(mesh(new THREE.BoxGeometry(bw > bd ? bw : 0.03, 0.8, bd > bw ? bd : 0.03), mat(GLASS, { transparent: true, opacity: 0.3, depthWrite: false, roughness: 0.1 }), cx, 0.95, cz)))
      const len = Math.max(bw, bd), n = Math.max(1, Math.round(len / 1.0))
      for (let i = 0; i <= n; i++) {
        const u = (bw > bd ? b.x0 : b.z0) + (len * i) / n
        g.add(box(0.06, 0.8, 0.06, '#fffaf0', bw > bd ? u : cx, 0.95, bw > bd ? cz : u))
      }
    } else g.add(rbox(bw, WALL_H, bd, WALL, cx, 0, cz, 0.03))
    g.add(rbox(bw + 0.04, 0.06, bd + 0.04, CAP, cx, WALL_H, cz, 0.02))
  }
  // the exit door (south wall, facing into the room)
  g.add(doorFrame({ x: r.door, z: z1 - 0.08, out: [0, -1], color }))
  // outside the windows: grass, a few round trees
  for (const s of windows) {
    const out = s === 'north' ? [0, -1] : s === 'west' ? [-1, 0] : [1, 0]
    for (let i = -2; i <= 2; i++) {
      const along = i * 1.6 + 0.4
      const x = s === 'north' ? along : (s === 'west' ? x0 : x1) + out[0] * 1.6
      const z = s === 'north' ? z0 - 1.6 : along * 0.7
      g.add(cyl(0.06, 0.08, 0.6, '#9a6a44', x, 0, z, 6), ball(0.42 + (i & 1) * 0.1, i % 2 ? '#7fae5a' : '#6f9a4f', x, 0.85, z, 1))
    }
  }
  g.add(flat(rbox(w + 9, 0.02, d + 9, '#cfe0b4', (x0 + x1) / 2, -0.33, (z0 + z1) / 2, 0.3)))
  return { statics: mergeStatic(g), floor }
}

function Sky({ r, windows }: { r: Room; windows: ('north' | 'west' | 'east')[] }) {
  const tex = useMemo(() => skyTexture(), [])
  const { x0, x1, z0, z1 } = r.floor
  return (
    <>
      {windows.map((s) => {
        const pos: [number, number, number] = s === 'north' ? [(x0 + x1) / 2, 1.6, z0 - 4] : s === 'west' ? [x0 - 4, 1.6, (z0 + z1) / 2] : [x1 + 4, 1.6, (z0 + z1) / 2]
        const rot = s === 'north' ? 0 : s === 'west' ? Math.PI / 2 : -Math.PI / 2
        return (
          <mesh key={s} position={pos} rotation={[0, rot, 0]}>
            <planeGeometry args={[18, 5]} />
            <meshBasicMaterial map={tex} toneMapped={false} />
          </mesh>
        )
      })}
    </>
  )
}

/** 休息區: half court (hoop on the north wall, a ball bouncing), a bench, a vending machine, a little table with
 *  stools, a plant, and one co-worker on a tea break. */
const BREAK_WIN: ('north' | 'west')[] = ['north', 'west']
export function BreakRoom() {
  const WIN = BREAK_WIN
  const { shell, extra, ballMesh, mate } = useMemo(() => {
    const shell = roomShell(BREAK_ROOM, WIN, '#eef2e2', '#dfe6cf', '#8fd16a')
    const g = new THREE.Group()
    const C = BREAK.court, z0 = BREAK_ROOM.floor.z0
    const paint = (w: number, d: number, color: string, x: number, z: number, y = 0.004) => g.add(flat(box(w, 0.006, d, color, x, y, z)))
    const ccx = (C.x0 + C.x1) / 2
    paint(C.x1 - C.x0, C.z1 - C.z0, '#ecb98f', ccx, (C.z0 + C.z1) / 2, 0.005)
    const line = (w: number, d: number, x: number, z: number) => paint(w, d, '#fffaf0', x, z, 0.007)
    line(C.x1 - C.x0, 0.05, ccx, C.z1); line(0.05, C.z1 - C.z0, C.x0, (C.z0 + C.z1) / 2); line(0.05, C.z1 - C.z0, C.x1, (C.z0 + C.z1) / 2)
    paint(1.2, 1.6, '#e3a477', C.hoopX, C.z0 + 0.8, 0.006)
    line(1.2, 0.05, C.hoopX, C.z0 + 1.6); line(0.05, 1.6, C.hoopX - 0.6, C.z0 + 0.8); line(0.05, 1.6, C.hoopX + 0.6, C.z0 + 0.8)
    const arc = flat(mesh(new THREE.RingGeometry(1.75, 1.8, 32, 1, Math.PI, Math.PI), mat('#fffaf0'), C.hoopX, 0.008, C.z0 + 0.15)); arc.rotation.x = -Math.PI / 2; g.add(arc)
    // hoop: pole, arm, backboard, rim, a white net
    g.add(cyl(0.05, 0.06, 1.7, '#6b7078', C.hoopX, 0.85, z0 + 0.3, 8), rbox(0.3, 0.08, 0.5, '#6b7078', C.hoopX, 0, z0 + 0.35, 0.03))
    g.add(box(0.05, 0.05, 0.4, '#6b7078', C.hoopX, 1.62, z0 + 0.5))
    g.add(rbox(0.8, 0.5, 0.05, '#fffaf0', C.hoopX, 1.4, z0 + 0.7, 0.03), box(0.3, 0.2, 0.06, '#e07a74', C.hoopX, 1.52, z0 + 0.7))
    const rim = mesh(new THREE.TorusGeometry(0.14, 0.018, 6, 16), mat('#e8743b'), C.hoopX, 1.45, z0 + 0.88); rim.rotation.x = Math.PI / 2; g.add(rim)
    g.add(cyl(0.14, 0.09, 0.18, '#fffaf0', C.hoopX, 1.36, z0 + 0.88, 8))
    // bench, vending machine, table + stools, plant
    const B = BREAK.bench
    g.add(rbox(B.w, 0.06, B.d, WOOD, B.x, 0.24, B.z, 0.03))
    for (const dz of [-0.55, 0.55]) g.add(box(B.w - 0.06, 0.24, 0.06, '#8a6a4a', B.x, 0.12, B.z + dz))
    const V = BREAK.vending
    g.add(rbox(0.42, 1.15, 0.5, '#e07a74', V.x, 0, V.z, 0.05), rbox(0.02, 0.55, 0.34, '#cfe6ee', V.x + 0.22, 0.45, V.z, 0.01))
    for (let i = 0; i < 3; i++) g.add(box(0.02, 0.08, 0.06, ['#f2c14e', '#6fa8dc', '#8fd16a'][i], V.x + 0.23, 0.6 + i * 0.13, V.z - 0.08))
    const T = BREAK.table
    g.add(cyl(0.38, 0.38, 0.04, '#fffaf0', T.x, 0.46, T.z, 14), cyl(0.04, 0.06, 0.46, '#6b7078', T.x, 0.23, T.z, 8))
    for (const [dx, dz] of [[-0.55, 0], [0.55, 0.1]]) g.add(cyl(0.14, 0.14, 0.04, '#6fa8dc', T.x + dx, 0.28, T.z + dz, 10), cyl(0.03, 0.03, 0.28, '#6b7078', T.x + dx, 0.14, T.z + dz, 6))
    g.add(cyl(0.035, 0.03, 0.08, '#fffaf0', T.x + 0.1, 0.52, T.z - 0.08, 8))
    const P = BREAK.plant
    g.add(cyl(0.16, 0.12, 0.28, '#d98a5f', P.x, 0.14, P.z, 8), ball(0.28, '#6f9a4f', P.x, 0.5, P.z, 1), ball(0.18, '#7fae5a', P.x + 0.1, 0.72, P.z - 0.05, 1))
    // a wall clock over the vending machine
    const clock = mesh(new THREE.CylinderGeometry(0.16, 0.16, 0.03, 18), mat('#fffaf0'), BREAK_ROOM.floor.x0 + 0.1, 1.5, -1.2); clock.rotation.z = Math.PI / 2; g.add(clock)
    // a ball that bounces under the hoop
    const ballMesh = ball(0.12, '#e8743b', C.hoopX - 0.5, 0.12, C.z0 + 1.9, 1)
    // a co-worker at the table with a cup of tea
    const mate = makeFigure({ shirt: '#e8c15a', pants: '#3f4a5a', hair: '#2a1e14', vest: '#ff8a3d' })
    mate.root.scale.setScalar(0.85)
    mate.root.position.set(T.x - 0.55, 0, T.z + 0.55)
    mate.root.rotation.y = Math.PI * 0.85
    restPose(mate)
    mate.armR.rotation.x = -1.0
    mate.handR.add(cyl(0.035, 0.03, 0.08, '#fffaf0', 0, -0.02, 0.03, 8))
    return { shell, extra: mergeStatic(g), ballMesh, mate }
  }, [])
  useFrame(({ clock }) => {
    const t = clock.elapsedTime
    ballMesh.position.y = 0.12 + Math.abs(Math.sin(t * 2.6)) * 0.55
    ballMesh.rotation.x = t * 2
    mate.body.position.y = 0.36 + Math.sin(t * 2) * 0.006
    mate.armR.rotation.x = -1.0 - Math.max(0, Math.sin(t * 0.7)) * 0.6
  })
  const r = BREAK_ROOM
  return (
    <group>
      <primitive object={shell.statics} />
      <primitive object={shell.floor} />
      <primitive object={extra} />
      <primitive object={ballMesh} />
      <primitive object={mate.root} />
      <Sky r={r} windows={WIN} />
      <Roof b={r.floor} y={2.2} wallTop={WALL_H + 0.06} lamps={ROOM_LAMPS} />
      <RoomSign id="here-break" label="休息區" x={-1.2} z={r.floor.z0 + 0.15} y={1.6} />
      <RoomSign id="exit-break" label="回到工廠" door="#8fd16a" x={r.door} z={r.floor.z1 - 0.2} y={1.52} />
    </group>
  )
}

/** 廁所: three stalls (one door ajar, two showing a little green "free" sign), sinks under a long mirror, a hand
 *  dryer, a plant. */
const NO_WIN: ('north' | 'west' | 'east')[] = []
export function Restroom() {
  const WIN = NO_WIN
  const { shell, extra } = useMemo(() => {
    const shell = roomShell(TOILET_ROOM, WIN, '#e3f0f4', '#cfe1e8', '#6fa8dc')
    const g = new THREE.Group()
    const { x1, z0 } = TOILET_ROOM.floor
    const STALL_D = 1.1, STALL = '#a9c9d6', DOOR = '#8fbfd6'
    // partitions between and around the stalls
    for (const s of TOILET.stalls) {
      for (const dx of [-0.5, 0.5]) g.add(rbox(0.05, 1.25, STALL_D, STALL, s.x + dx, 0.08, z0 + STALL_D / 2, 0.01))
      // toilet: bowl + tank + seat
      g.add(rbox(0.32, 0.3, 0.38, '#ffffff', s.x, 0, z0 + 0.42, 0.1), rbox(0.36, 0.34, 0.14, '#ffffff', s.x, 0.2, z0 + 0.14, 0.04))
      g.add(rbox(0.3, 0.03, 0.34, '#f2f2f2', s.x, 0.3, z0 + 0.44, 0.1))
      g.add(box(0.12, 0.12, 0.04, '#fffaf0', s.x - 0.3, 0.55, z0 + 0.04))   // paper roll holder
    }
    // stall doors: the middle one ajar, the others shut with a little green 無人 tab
    TOILET.stalls.forEach((s, i) => {
      if (i === 1) {
        const leaf = box(0.9, 1.15, 0.04, DOOR, 0, 0, 0)
        const pivot = new THREE.Group(); pivot.position.set(s.x - 0.45, 0.6, z0 + STALL_D)
        leaf.position.set(0.45, 0, 0); pivot.add(leaf); pivot.rotation.y = 1.1; g.add(pivot)   // swung into the stall
      } else {
        g.add(box(0.9, 1.15, 0.04, DOOR, s.x, 0.6, z0 + STALL_D))
        g.add(box(0.12, 0.06, 0.02, '#3fcf5a', s.x + 0.3, 0.75, z0 + STALL_D + 0.03))
      }
      g.add(box(0.95, 0.06, 0.06, STALL, s.x, 1.33, z0 + STALL_D))
    })
    // sink counter on the east wall, three basins with taps, a long mirror above
    g.add(rbox(0.5, 0.06, 2.4, '#fffaf0', x1 - 0.27, 0.62, 0.4, 0.03), rbox(0.45, 0.6, 2.35, '#d6e4ea', x1 - 0.27, 0, 0.4, 0.03))
    for (const s of TOILET.sinks) {
      g.add(cyl(0.16, 0.12, 0.08, '#ffffff', s.x, 0.66, s.z, 14))
      g.add(cyl(0.02, 0.02, 0.16, '#a9b2ba', x1 - 0.12, 0.76, s.z, 6), box(0.12, 0.02, 0.02, '#a9b2ba', x1 - 0.16, 0.84, s.z))
    }
    g.add(rbox(0.04, 0.7, 2.3, '#9fb4bf', x1 - 0.1, 0.85, 0.4, 0.02))
    g.add(flat(mesh(new THREE.BoxGeometry(0.02, 0.62, 2.2), mat('#e9f6fb', { roughness: 0.05, metalness: 0.3 }), x1 - 0.12, 1.2, 0.4)))
    // hand dryer + a towel dispenser on the west wall, the plant in the corner
    const { x0, z1 } = TOILET_ROOM.floor
    g.add(rbox(0.12, 0.26, 0.3, '#fffaf0', x0 + 0.14, 0.85, 0.2, 0.04), rbox(0.12, 0.3, 0.26, '#d6e4ea', x0 + 0.14, 0.75, 0.8, 0.04))
    g.add(cyl(0.14, 0.1, 0.24, '#d98a5f', -2.2, 0.12, z1 - 0.3, 8), ball(0.24, '#6f9a4f', -2.2, 0.42, z1 - 0.3, 1))
    // a yellow wet-floor sign, for the toy look
    g.add(rbox(0.22, 0.34, 0.05, YELLOW, 1.0, 0, 1.1, 0.02))
    return { shell, extra: mergeStatic(g) }
  }, [])
  const r = TOILET_ROOM
  return (
    <group>
      <primitive object={shell.statics} />
      <primitive object={shell.floor} />
      <primitive object={extra} />
      <Roof b={r.floor} y={2.2} wallTop={WALL_H + 0.06} lamps={ROOM_LAMPS} />
      <RoomSign id="here-toilet" label="廁所" x={0.5} z={r.floor.z0 + 1.15} y={1.5} />
      <RoomSign id="exit-toilet" label="回到工廠" door="#6fa8dc" x={r.door} z={r.floor.z1 - 0.2} y={1.52} />
    </group>
  )
}
