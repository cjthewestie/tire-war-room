// Low-poly factory hall: floor, walls, line zones, 28 machines with live status lights, war-room desk + screen.
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
  DESK, HALL, LIGHT_HEX, LINE_HEX, PLACED, SCREEN, WAR_ROOM, statusLight, type MachineKind, type Placed,
} from './layout'
import { player } from './pose'

// ---- shared low-poly materials (flat shading, no textures) ----
const mat = (color: string, extra: THREE.MeshStandardMaterialParameters = {}) =>
  new THREE.MeshStandardMaterial({ color, flatShading: true, roughness: 0.85, metalness: 0.05, ...extra })
const M = {
  body: mat('#5b6573'), bodyDark: mat('#3b434e'), steel: mat('#9aa4b1', { metalness: 0.3, roughness: 0.6 }),
  roll: mat('#2a2f36', { metalness: 0.4, roughness: 0.5 }), yellow: mat('#e0b43a'), rubber: mat('#1d1f22'),
  floor: mat('#4a5058'), wall: mat('#3d4652'), beam: mat('#58626f'), desk: mat('#59422e'),
  deskTop: mat('#2d3238'), plate: mat('#22303a'), pole: mat('#252a30'), glass: mat('#88b4d8', { transparent: true, opacity: 0.12 }),
}

/** Low-poly geometry per machine kind, built from a few primitives, centred on the footprint, base at y=0. */
function MachineBody({ kind, w, d, h }: { kind: MachineKind; w: number; d: number; h: number }) {
  switch (kind) {
    case 'mixer':
      return (<group>
        <mesh material={M.body} position={[0, h * 0.35, 0]}><boxGeometry args={[w, h * 0.7, d]} /></mesh>
        <mesh material={M.yellow} position={[0, h * 0.85, 0]}><cylinderGeometry args={[w * 0.15, w * 0.42, h * 0.3, 6]} /></mesh>
      </group>)
    case 'mill': case 'bigmill':
      return (<group>
        <mesh material={M.body} position={[0, h * 0.3, 0]}><boxGeometry args={[w, h * 0.6, d]} /></mesh>
        {[-0.22, 0.22].map((oz) => (
          <mesh key={oz} material={M.roll} position={[0, h * 0.75, oz * d]} rotation={[0, 0, Math.PI / 2]}>
            <cylinderGeometry args={[d * 0.2, d * 0.2, w * 0.9, 8]} />
          </mesh>))}
        {[-0.5, 0.5].map((ox) => <mesh key={ox} material={M.yellow} position={[ox * w, h * 0.65, 0]}><boxGeometry args={[0.2, h * 0.7, d * 0.9]} /></mesh>)}
      </group>)
    case 'calender':
      return (<group>
        {[-0.5, 0.5].map((ox) => <mesh key={ox} material={M.body} position={[ox * (w - 0.3), h / 2, 0]}><boxGeometry args={[0.3, h, d]} /></mesh>)}
        {[0.3, 0.55, 0.8].map((fy) => (
          <mesh key={fy} material={M.roll} position={[0, h * fy, 0]} rotation={[0, 0, Math.PI / 2]}><cylinderGeometry args={[0.28, 0.28, w - 0.4, 8]} /></mesh>))}
      </group>)
    case 'cooler':
      return (<group>
        <mesh material={M.steel} position={[0, h * 0.5, 0]}><boxGeometry args={[w, h, d]} /></mesh>
        {[-0.3, 0, 0.3].map((ox) => <mesh key={ox} material={M.bodyDark} position={[ox * w, h + 0.2, 0]}><boxGeometry args={[0.6, 0.4, d * 0.7]} /></mesh>)}
      </group>)
    case 'winder': case 'payoff':
      return (<group>
        <mesh material={M.body} position={[0, 0.2, 0]}><boxGeometry args={[w, 0.4, d]} /></mesh>
        <mesh material={kind === 'payoff' ? M.yellow : M.roll} position={[0, h * 0.6, 0]} rotation={[Math.PI / 2, 0, 0]}>
          <cylinderGeometry args={[Math.min(w, h) * 0.38, Math.min(w, h) * 0.38, d * 0.6, 10]} />
        </mesh>
      </group>)
    case 'hydraulic':
      return (<group>
        <mesh material={M.body} position={[0, 0.3, 0]}><boxGeometry args={[w, 0.6, d]} /></mesh>
        {[[-1, -1], [1, -1], [-1, 1], [1, 1]].map(([sx, sz]) => (
          <mesh key={`${sx}${sz}`} material={M.steel} position={[sx * w * 0.38, h * 0.5, sz * d * 0.38]}><cylinderGeometry args={[0.1, 0.1, h, 6]} /></mesh>))}
        <mesh material={M.yellow} position={[0, h * 0.85, 0]}><boxGeometry args={[w, h * 0.3, d]} /></mesh>
        <mesh material={M.steel} position={[0, h * 0.6, 0]}><cylinderGeometry args={[0.3, 0.3, h * 0.3, 8]} /></mesh>
      </group>)
    case 'extruder':
      return (<group>
        <mesh material={M.body} position={[0, 0.4, 0]}><boxGeometry args={[w, 0.8, d]} /></mesh>
        <mesh material={M.steel} position={[0, 1.05, 0]} rotation={[0, 0, Math.PI / 2]}><cylinderGeometry args={[0.35, 0.35, w * 0.95, 8]} /></mesh>
        <mesh material={M.yellow} position={[-w * 0.35, h * 0.95, 0]}><cylinderGeometry args={[0.15, 0.4, 0.6, 6]} /></mesh>
      </group>)
    case 'bladder':
      return (<group>
        <mesh material={M.body} position={[0, 0.3, 0]}><boxGeometry args={[w, 0.6, d]} /></mesh>
        <mesh material={M.rubber} position={[0, h * 0.6, 0]}><icosahedronGeometry args={[w * 0.4, 0]} /></mesh>
      </group>)
    case 'cutter':
      return (<group>
        <mesh material={M.body} position={[0, h * 0.4, 0]}><boxGeometry args={[w, h * 0.8, d]} /></mesh>
        <mesh material={M.steel} position={[0, h * 0.95, 0]}><boxGeometry args={[0.1, h * 0.3, d * 1.05]} /></mesh>
        <mesh material={M.yellow} position={[0, h + 0.3, 0]}><boxGeometry args={[w * 0.3, 0.3, d * 1.05]} /></mesh>
      </group>)
    case 'press':
      return (<group>
        <mesh material={M.bodyDark} position={[0, 0.5, 0]}><cylinderGeometry args={[w * 0.48, w * 0.5, 1.0, 8]} /></mesh>
        <mesh material={M.steel} position={[0, 1.35, 0]}><cylinderGeometry args={[w * 0.32, w * 0.46, 0.7, 8]} /></mesh>
        <mesh material={M.body} position={[0, h * 0.5, d * 0.45]}><boxGeometry args={[w * 0.9, h, 0.2]} /></mesh>
        <mesh material={M.steel} position={[w * 0.3, h * 0.55, -d * 0.3]}><cylinderGeometry args={[0.08, 0.08, h * 1.1, 5]} /></mesh>
      </group>)
  }
}

function Machine({ p, tile, hot }: { p: Placed; tile: MachineTile | undefined; hot: boolean }) {
  const light = statusLight(tile?.status)
  const lampRef = useRef<THREE.MeshStandardMaterial>(null)
  const ringRef = useRef<THREE.Mesh>(null)
  const [near, setNear] = useState(false)
  useFrame(({ clock }) => {
    const t = clock.elapsedTime
    if (lampRef.current) lampRef.current.emissiveIntensity = light === 'red' ? 1.2 + Math.sin(t * 9) * 1.0 : light === 'amber' ? 1.1 + Math.sin(t * 4) * 0.4 : 0.9
    if (ringRef.current) {
      const s = 1 + ((t * 0.8) % 1) * 0.6
      ringRef.current.scale.set(s, s, 1)
      ;(ringRef.current.material as THREE.MeshBasicMaterial).opacity = 0.7 * (1 - ((t * 0.8) % 1))
    }
    const dist = Math.hypot(player.x - p.x, player.z - p.z)
    const n = dist < 7.5
    if (n !== near) setNear(n)
  })
  const lampY = p.h + 0.9
  const temp = tile?.temp_c
  return (
    <group position={[p.x, 0, p.z]} name={p.id}>
      <MachineBody kind={p.kind} w={p.w} d={p.d} h={p.h} />
      <mesh material={M.pole} position={[p.w / 2 - 0.15, (p.h + lampY) / 2, p.d / 2 - 0.15]}><cylinderGeometry args={[0.04, 0.04, lampY - p.h + 0.4, 4]} /></mesh>
      <mesh position={[p.w / 2 - 0.15, lampY, p.d / 2 - 0.15]}>
        <icosahedronGeometry args={[0.2, 0]} />
        <meshStandardMaterial ref={lampRef} color={LIGHT_HEX[light]} emissive={LIGHT_HEX[light]} flatShading />
      </mesh>
      {light !== 'green' && light !== 'grey' && (
        <pointLight color={LIGHT_HEX[light]} intensity={6} distance={6} decay={2} position={[0, p.h + 0.5, 0]} />
      )}
      {hot && (
        <mesh ref={ringRef} rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.03, 0]}>
          <ringGeometry args={[Math.max(p.w, p.d) * 0.7, Math.max(p.w, p.d) * 0.7 + 0.25, 24]} />
          <meshBasicMaterial color={LIGHT_HEX.red} transparent opacity={0.6} depthWrite={false} />
        </mesh>
      )}
      {near && (
        <Html position={[0, p.h + 0.55, 0]} center distanceFactor={9} zIndexRange={[5, 0]} style={{ pointerEvents: 'none' }}>
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

/** A sign readable from both sides: two back-to-back planes, so neither face is mirrored. */
function Sign({ map, w, h, position, yaw }: { map: THREE.Texture; w: number; h: number; position: [number, number, number]; yaw: number }) {
  return (
    <group position={position} rotation={[0, yaw, 0]}>
      <mesh><planeGeometry args={[w, h]} /><meshBasicMaterial map={map} transparent /></mesh>
      <mesh rotation={[0, Math.PI, 0]}><planeGeometry args={[w, h]} /><meshBasicMaterial map={map} transparent /></mesh>
    </group>
  )
}

const FONT = '"Noto Sans TC", "PingFang TC", "Heiti TC", sans-serif'

function LineZone({ line }: { line: Line }) {
  const ms = PLACED.filter((p) => p.line === line)
  const x0 = Math.min(...ms.map((p) => p.x - p.w / 2)) - 1.2
  const x1 = Math.max(...ms.map((p) => p.x + p.w / 2)) + 1.2
  const z = ms[0].z
  const d = Math.max(...ms.map((p) => p.d)) + 1.6
  const label = useTextTexture(512, 128, (g) => {
    g.fillStyle = LINE_HEX[line]
    g.font = `bold 64px ${FONT}`
    g.textBaseline = 'middle'
    g.fillText(LINE_LABELS[line], 8, 64)
  }, line)
  return (
    <group>
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[(x0 + x1) / 2, 0.012, z]}>
        <planeGeometry args={[x1 - x0, d]} />
        <meshStandardMaterial color={LINE_HEX[line]} transparent opacity={0.1} depthWrite={false} />
      </mesh>
      {/* yellow-black safety edge lines */}
      {[-1, 1].map((s) => (
        <mesh key={s} rotation={[-Math.PI / 2, 0, 0]} position={[(x0 + x1) / 2, 0.015, z + (s * d) / 2]}>
          <planeGeometry args={[x1 - x0, 0.12]} />
          <meshBasicMaterial color="#e0b43a" />
        </mesh>
      ))}
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[x0 + 2.3, 0.02, z - d / 2 - 0.7]}>
        <planeGeometry args={[4.4, 1.1]} />
        <meshBasicMaterial map={label} transparent depthWrite={false} />
      </mesh>
      {/* hanging sign over the line */}
      <Sign map={label} w={4.0} h={1.0} position={[x0 + 2.5, 5.2, z]} yaw={Math.PI / 2} />
    </group>
  )
}

function Hall() {
  const W = HALL.x1 - HALL.x0
  const D = HALL.z1 - HALL.z0
  const cx = (HALL.x0 + HALL.x1) / 2
  const cz = (HALL.z0 + HALL.z1) / 2
  return (
    <group>
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[cx, 0, cz]} material={M.floor}><planeGeometry args={[W, D]} /></mesh>
      <gridHelper args={[Math.max(W, D), Math.round(Math.max(W, D) / 2), '#4a5058', '#40464d']} position={[cx, 0.005, cz]} />
      {/* walls */}
      <mesh material={M.wall} position={[cx, HALL.h / 2, HALL.z0 - 0.2]}><boxGeometry args={[W + 0.8, HALL.h, 0.4]} /></mesh>
      <mesh material={M.wall} position={[cx, HALL.h / 2, HALL.z1 + 0.2]}><boxGeometry args={[W + 0.8, HALL.h, 0.4]} /></mesh>
      <mesh material={M.wall} position={[HALL.x0 - 0.2, HALL.h / 2, cz]}><boxGeometry args={[0.4, HALL.h, D]} /></mesh>
      <mesh material={M.wall} position={[HALL.x1 + 0.2, HALL.h / 2, cz]}><boxGeometry args={[0.4, HALL.h, D]} /></mesh>
      {/* roof trusses + light strips */}
      {Array.from({ length: 8 }, (_, i) => HALL.x0 + 3 + i * ((W - 6) / 7)).map((x) => (
        <group key={x}>
          <mesh material={M.beam} position={[x, HALL.h - 0.3, cz]}><boxGeometry args={[0.3, 0.4, D]} /></mesh>
          <mesh position={[x + 1.2, HALL.h - 0.6, cz]}><boxGeometry args={[0.25, 0.08, D - 4]} /><meshBasicMaterial color="#dfe8f0" /></mesh>
        </group>
      ))}
      {/* columns */}
      {[-14, 0, 14].flatMap((x) => [HALL.z0 + 0.3, HALL.z1 - 0.3].map((z) => (
        <mesh key={`${x}${z}`} material={M.beam} position={[x, HALL.h / 2, z]}><boxGeometry args={[0.5, HALL.h, 0.5]} /></mesh>
      )))}
    </group>
  )
}

function ScreenTexture({ state, connected, mode }: { state: WarRoomState; connected: boolean; mode: string }) {
  const snap = state.snapshot
  const ev: Event | null = primaryEvent(snap?.events)
  const ft = state.clock?.factory_time ?? snap?.header.factory_time
  const key = JSON.stringify([snap?.machines.map((m) => m.status + (m.temp_c ?? '')), ev?.id, ev?.state, state.pending.length, ft?.slice(0, 16), connected])
  const tex = useTextTexture(1280, 720, (g) => {
    g.fillStyle = '#0f1115'; g.fillRect(0, 0, 1280, 720)
    g.fillStyle = '#76b900'; g.fillRect(0, 0, 1280, 8)
    g.textBaseline = 'top'
    g.fillStyle = '#e8eaed'; g.font = `bold 52px ${FONT}`
    g.fillText('實心胎智慧製造戰情室', 40, 32)
    g.font = `30px ${FONT}`; g.fillStyle = '#9aa0a6'
    g.fillText(`${ft ? `${fmtMD(ft)} ${fmtHM(ft)}` : '—'}  ·  ${connected ? (mode === 'mock' ? '模擬資料' : '即時連線') : '連線中…'}`, 40, 100)
    const h = snap?.header
    g.font = `bold 44px ${FONT}`
    g.fillStyle = '#46d160'; g.fillText(`正常 ${h?.normal_count ?? '—'}`, 40, 160)
    g.fillStyle = (h?.abnormal_count ?? 0) > 0 ? '#ff6b5e' : '#9aa0a6'; g.fillText(`異常 ${h?.abnormal_count ?? '—'}`, 300, 160)
    g.fillStyle = state.pending.length ? '#f2b33d' : '#9aa0a6'; g.fillText(`待核准 ${state.pending.length}`, 560, 160)
    g.font = `34px ${FONT}`; g.fillStyle = ev && ev.state !== 'closed' ? '#ff6b5e' : '#9aa0a6'
    g.fillText(ev ? `事件：${machineLabel(ev.machine_id)}・${STATE_LABEL[ev.state]}` : '目前無事件', 40, 240)
    if (ev?.alert_card) { g.fillStyle = '#e8eaed'; g.font = `28px ${FONT}`; g.fillText(ev.alert_card.title.slice(0, 40), 40, 290) }
    // mini map: one dot per machine, coloured by status
    const ms = snap?.machines ?? []
    LINES.forEach((line, li) => {
      g.fillStyle = LINE_HEX[line]; g.font = `bold 26px ${FONT}`
      g.fillText(line === 'BOND' ? '貼合' : line === 'CURE' ? '硫化' : line, 40, 370 + li * 56)
      ms.filter((m) => m.line === line).forEach((m, i) => {
        g.fillStyle = LIGHT_HEX[statusLight(m.status)]
        g.beginPath(); g.arc(150 + i * 64, 384 + li * 56, 18, 0, Math.PI * 2); g.fill()
        g.fillStyle = '#0f1115'; g.font = `bold 15px ${FONT}`; g.textAlign = 'center'
        g.fillText(m.short_name.slice(0, 2), 150 + i * 64, 376 + li * 56); g.textAlign = 'left'
      })
    })
  }, key)
  return <meshBasicMaterial map={tex} toneMapped={false} />
}

function WarRoom({ state, connected, mode }: { state: WarRoomState; connected: boolean; mode: string }) {
  const w = WAR_ROOM.x1 - WAR_ROOM.x0
  const d = WAR_ROOM.z1 - WAR_ROOM.z0
  const sign = useTextTexture(512, 128, (g) => {
    g.fillStyle = '#76b900'; g.font = `bold 72px ${FONT}`; g.textBaseline = 'middle'; g.fillText('戰情室', 8, 64)
  }, 'sign')
  return (
    <group>
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[(WAR_ROOM.x0 + WAR_ROOM.x1) / 2, 0.02, 0]} material={M.plate}><planeGeometry args={[w, d]} /></mesh>
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[WAR_ROOM.x0, 0.025, 0]}><planeGeometry args={[0.15, d]} /><meshBasicMaterial color="#76b900" /></mesh>
      {/* glass partitions on the two long sides, open toward the hall */}
      {[WAR_ROOM.z0, WAR_ROOM.z1].map((z) => (
        <group key={z}>
          <mesh material={M.glass} position={[(WAR_ROOM.x0 + WAR_ROOM.x1) / 2, 1.4, z]}><boxGeometry args={[w, 2.8, 0.05]} /></mesh>
          <mesh material={M.beam} position={[(WAR_ROOM.x0 + WAR_ROOM.x1) / 2, 2.82, z]}><boxGeometry args={[w, 0.08, 0.08]} /></mesh>
        </group>
      ))}
      {/* desk */}
      <group position={[DESK.x, 0, DESK.z]}>
        <mesh material={M.desk} position={[0, DESK.h / 2, 0]}><boxGeometry args={[DESK.w, DESK.h, DESK.d]} /></mesh>
        <mesh material={M.deskTop} position={[0, DESK.h + 0.03, 0]}><boxGeometry args={[DESK.w + 0.1, 0.06, DESK.d + 0.1]} /></mesh>
        {[-1.1, 0, 1.1].map((z) => (
          <group key={z} position={[0.25, DESK.h + 0.06, z]}>
            <mesh material={M.pole} position={[0, 0.2, 0]}><boxGeometry args={[0.06, 0.4, 0.06]} /></mesh>
            <mesh position={[0, 0.5, 0]} rotation={[0, -Math.PI / 2, 0]}><boxGeometry args={[0.9, 0.5, 0.04]} /><meshStandardMaterial color="#101418" emissive="#12301a" emissiveIntensity={0.5} /></mesh>
          </group>
        ))}
        <mesh material={M.bodyDark} position={[-0.1, DESK.h + 0.08, 0]}><boxGeometry args={[0.3, 0.03, 0.8]} /></mesh>
      </group>
      {/* big screen on the east wall */}
      <group position={[SCREEN.x, SCREEN.y, SCREEN.z]} rotation={[0, -Math.PI / 2, 0]}>
        <mesh position={[0, 0, -0.06]} material={M.pole}><boxGeometry args={[SCREEN.w + 0.3, SCREEN.h + 0.3, 0.1]} /></mesh>
        <mesh><planeGeometry args={[SCREEN.w, SCREEN.h]} /><ScreenTexture state={state} connected={connected} mode={mode} /></mesh>
      </group>
      <Sign map={sign} w={3.2} h={0.8} position={[WAR_ROOM.x0 + 0.1, 3.4, 0]} yaw={-Math.PI / 2} />
    </group>
  )
}

export function World({ state, connected, mode }: { state: WarRoomState; connected: boolean; mode: string }) {
  const tiles = useMemo(() => Object.fromEntries((state.snapshot?.machines ?? []).map((m) => [m.machine_id, m])), [state.snapshot?.machines])
  const hot = useMemo(() => new Set((state.snapshot?.events ?? []).filter((e) => e.state !== 'closed' && e.state !== 'safe_stop').map((e) => e.machine_id)), [state.snapshot?.events])
  return (
    <group>
      <hemisphereLight args={['#e8eef7', '#3a3f46', 1.6]} />
      <directionalLight position={[10, 20, 8]} intensity={1.4} />
      <ambientLight intensity={0.4} />
      <Hall />
      {LINES.map((l) => <LineZone key={l} line={l} />)}
      {PLACED.map((p) => <Machine key={p.id} p={p} tile={tiles[p.id]} hot={hot.has(p.id)} />)}
      <WarRoom state={state} connected={connected} mode={mode} />
    </group>
  )
}
