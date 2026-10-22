// Task workers (issue #17; everyone else stands by, two patrol) and the dock truck, driven by the live world (GET /api/world, WS "world"; issue #11).
// Every on-duty worker is a chibi figure that walks (along the aisles and the east corridor) to where the sim
// says they are: at their machine, in the war room, the staff corner, the dock; going to the 休息區 or 廁所 they
// walk to that door and step through it. The truck at the dock drives in when a delivery or a shipment arrives
// and drives off when it leaves. Light on purpose: no pathfinding, a small fixed aisle graph.
import { useFrame } from '@react-three/fiber'
import { useEffect, useMemo, useRef } from 'react'
import * as THREE from 'three'
import type { WorldSnapshot, WorldTruck, WorldWorker } from '../contracts'
import { box, cyl, makeFigure, rbox, restPose, walkCycle, type Figure } from './kit'
import { REPAIR_KINDS } from '../aiStatus'
import { DESK, DOCK, PLACED_BY_ID, STAFF_SPOTS, WALL_DOORS } from './layout'

type P = [number, number]
const AISLES = [-3.5, -1.3, 0.9, 3.25, 5.6]
const CORRIDOR_X = 4.2
const WAR_DOOR: P = [5.3, 0]
const STAFF_GAP: P[] = [[9.1, 2.0], [10.1, 2.0]]
const LOADING: P = [7.1, 3.7]
const BIG_DOOR: P = [7.1, 5.9]
const LOBBY: P = [6.2, -4.2]
const BREAK_DOOR = WALL_DOORS.find((d) => d.to === 'break')!
const TOILET_DOOR = WALL_DOORS.find((d) => d.to === 'toilet')!
const SPEED = 1.15                // figure heights per second
const TRUCK_PARK: P = [7.1, DOCK.z1 + 1.15]
const TRUCK_AWAY = 13

const nearestAisle = (z: number) => AISLES.reduce((a, b) => (Math.abs(b - z) < Math.abs(a - z) ? b : a))
const east = (p: P) => p[0] >= 4.8
type Area = 'hall' | 'war' | 'staff' | 'loading' | 'dock' | 'lobby'
function area([x, z]: P): Area {
  if (z > 6.1) return 'dock'
  if (!east([x, z])) return 'hall'
  if (z < -2.4) return 'lobby'
  if (z > 2.4) return 'loading'
  return x >= 9.6 ? 'staff' : 'war'
}

/** Waypoints from a to b over the aisle graph (hall aisles ↔ the east corridor ↔ the east rooms). */
function route(a: P, b: P): P[] {
  const out: P[] = []
  const toHub = (p: P, rev: boolean): P[] => {
    // from p to a point on the corridor (x = CORRIDOR_X), and the reverse
    const ar = area(p)
    let pts: P[]
    if (ar === 'hall') { const z = nearestAisle(p[1]); pts = [[p[0], z], [CORRIDOR_X, z]] }
    else if (ar === 'war') pts = [WAR_DOOR, [CORRIDOR_X, 0]]
    else if (ar === 'staff') pts = [STAFF_GAP[1], STAFF_GAP[0], WAR_DOOR, [CORRIDOR_X, 0]]
    else if (ar === 'loading') pts = [LOADING, [CORRIDOR_X, 3.25]]
    else if (ar === 'dock') pts = [BIG_DOOR, LOADING, [CORRIDOR_X, 3.25]]
    else pts = [LOBBY, [CORRIDOR_X, -3.5]]
    return rev ? pts.reverse() : pts
  }
  const aa = area(a), ab = area(b)
  if (aa === ab && aa !== 'hall') return [b]
  if (aa === 'hall' && ab === 'hall') {
    const za = nearestAisle(a[1]), zb = nearestAisle(b[1])
    if (za === zb) return [[a[0], za], [b[0], za], b]
    return [[a[0], za], [CORRIDOR_X, za], [CORRIDOR_X, zb], [b[0], zb], b]
  }
  out.push(...toHub(a, false), ...toHub(b, true), b)
  return out
}

/** Where a worker stands for their state and zone. `vanish`: they leave the hall there (a door). */
function spot(w: WorldWorker, k: number): { p: P; yaw: number; vanish: boolean } {
  const jitter = ((k * 37) % 7) / 7 - 0.5
  if (w.zone === 'break') return { p: [BREAK_DOOR.x, BREAK_DOOR.z + 0.25], yaw: Math.PI, vanish: true }
  if (w.zone === 'toilet') return { p: [TOILET_DOOR.x - 0.25, TOILET_DOOR.z], yaw: -Math.PI / 2, vanish: true }
  if (w.zone === 'off_site') return { p: [BIG_DOOR[0] + jitter, DOCK.z1 - 0.2], yaw: 0, vanish: true }
  if (w.zone === 'war_room') return { p: [DESK.x - 0.6, DESK.z + jitter * 0.8], yaw: Math.PI / 2 + Math.PI, vanish: false }
  if (w.zone === 'staff') {
    const [x, z, yaw] = STAFF_SPOTS[k % STAFF_SPOTS.length]
    return { p: [x + (k >= 4 ? 0.35 : 0), z], yaw: yaw + Math.PI, vanish: false }
  }
  if (w.zone === 'dock') {
    const spots: P[] = [[6.0, 6.9], [8.2, 6.6], [6.8, 7.4], [7.6, 6.9], [5.6, 4.0], [8.6, 3.6]]
    return { p: spots[k % spots.length], yaw: 0, vanish: false }
  }
  const m = w.machine_id ? PLACED_BY_ID[w.machine_id] : undefined
  if (m) {
    const side = m.line === 'CURE' ? -1 : 1
    return { p: [m.x + jitter * Math.min(0.5, m.w * 0.6), m.z + side * (m.d / 2 + 0.32)], yaw: side > 0 ? Math.PI : 0, vanish: false }
  }
  // a line zone without a machine: the aisle south of the row
  return { p: [-2 + jitter * 4, 0.9], yaw: 0, vanish: false }
}

const LOOKS = [
  { shirt: '#6fa8dc', pants: '#4a5a6a', hat: '#f2c14e', vest: '#ff8a3d' },
  { shirt: '#8fbf5a', pants: '#5b4a3a', hair: '#7a4a24', vest: '#ff8a3d' },
  { shirt: '#a993d6', pants: '#4a4a5a', hat: '#f6f1e8' },
  { shirt: '#e8c15a', pants: '#3f4a5a', hair: '#2a1e14', vest: '#ff8a3d' },
  { shirt: '#e88f7a', pants: '#4a4a5a', hat: '#f2c14e' },
  { shirt: '#7ac6c0', pants: '#5b4a3a', hair: '#2a1e14' },
]
const ROLE_LOOK: Record<string, number> = { 主管: 2, 維修: 4, 品管: 5 }

interface Walker {
  f: Figure; path: P[]; yaw: number; key: string; phase: 'to' | 'at' | 'back'; task: THREE.Object3D
  ring: THREE.Mesh; progress: number; target: P; taskId: string; rush: boolean
}

/** A worker sent on a repair (檢修, 放慢投料…) hurries there at RUSH× walking speed: standby → 7 號 is ~17 units, ~15 s
 *  at SPEED, longer than the 12-second fast repair (REPAIR_FAST_S), so the machine came back before anyone reached it
 *  (呱呱 10-22). At RUSH× they are there in ~5 s, about when the server turns the task 執行中. */
const RUSH = 3.5

/** One frame of a task walker (kept out of the hooks: the walker lives in a ref, not in React state). True when a
 *  worker walking back has reached standby and leaves the scene (they are one of the staff corner again). */
function stepWalker(wk: Walker, t: number, dt: number, i: number, camQ: THREE.Quaternion): boolean {
  const r = wk.f.root
  wk.ring.quaternion.copy(camQ)
  wk.task.rotateY(dt * 2)
  if (wk.path.length) {
    const [tx, tz] = wk.path[0]
    const dx = tx - r.position.x, dz = tz - r.position.z
    const d = Math.hypot(dx, dz)
    const fast = wk.rush && wk.phase === 'to'
    // the server already has them there (執行中): catch up at once
    const step = SPEED * dt * (fast ? (wk.key.startsWith('執行中') ? RUSH * 2 : RUSH) : 1)
    if (d <= step) { r.position.set(tx, 0, tz); wk.path.shift() } else {
      r.position.x += (dx / d) * step
      r.position.z += (dz / d) * step
      r.rotation.y = Math.atan2(dx, dz)
    }
    walkCycle(wk.f, t, fast ? 16 : 10, 0.5)
    if (!wk.path.length) {
      restPose(wk.f)
      if (wk.phase === 'back') return true
      wk.phase = 'at'
    }
    return false
  }
  r.rotation.y = wk.yaw
  wk.f.body.position.y = 0.36 + Math.sin(t * 2 + i) * 0.007
  if (wk.key.startsWith('執行中')) wk.f.armR.rotation.x = -0.6 + Math.sin(t * 3 + i) * 0.25
  return false
}

const STANDBY: P = [10.45, 0.1]          // 員工待命區: where a task worker comes from and goes back to
const ON_TASK = new Set(['前往中', '執行中'])

function ringGeo(p: number) {
  return new THREE.RingGeometry(0.07, 0.1, 24, 1, Math.PI / 2, -Math.max(0.02, Math.min(1, p)) * Math.PI * 2)
}

/** Workers on a real task (issue #17). Everyone else stands by in the staff corner (StaffCorner) and two patrol the
 *  aisles (Walkers): CJ, 2026-10-20 「恢復人力待命 有一兩個人巡邏」. A worker with a task (world state 前往中 / 執行中 and
 *  a matching item in world.tasks) leaves the staff corner, walks to the task's machine with a marker and a progress
 *  ring over the head, works there, and walks back into standby when the task is 完成. */
export function WorldPeople({ world }: { world: WorldSnapshot }) {
  const group = useMemo(() => new THREE.Group(), [])
  const walkers = useRef(new Map<string, Walker>())
  useEffect(() => {
    const tasks = world.tasks?.items ?? []
    const active = new Set<string>()
    world.people.workers.forEach((w, k) => {
      // the task's status leads: a worker's own state moves only when the factory clock steps, and the clock is held
      // while the model computes, so a fast repair's crew showed up 8 s late (呱呱 10-22)
      const t = tasks.find((x) => x.worker_id === w.id && ON_TASK.has(x.status))
      if (!t) return
      active.add(w.id)
      const s = spot(w, k)
      let wk = walkers.current.get(w.id)
      if (!wk) {
        const idx = ROLE_LOOK[w.role] ?? (k % 4)
        const f = makeFigure(LOOKS[idx])
        f.root.scale.setScalar(0.85)
        restPose(f)
        const task = new THREE.Mesh(new THREE.OctahedronGeometry(0.09), new THREE.MeshBasicMaterial({ color: '#f5b83d' }))
        task.position.set(0, 1.05, 0)
        f.root.add(task)
        const ring = new THREE.Mesh(ringGeo(0), new THREE.MeshBasicMaterial({ color: '#76b900', side: THREE.DoubleSide }))
        ring.position.set(0, 1.3, 0)
        f.root.add(ring)
        group.add(f.root)
        f.root.position.set(STANDBY[0], 0, STANDBY[1])
        wk = { f, path: route(STANDBY, s.p), yaw: s.yaw, key: '', phase: 'to', task, ring, progress: -1, target: s.p, taskId: t.id, rush: false }
        walkers.current.set(w.id, wk)
      } else if (wk.phase === 'back' || wk.taskId !== t.id || wk.target[0] !== s.p[0] || wk.target[1] !== s.p[1]) {
        // a new task (or the same one again after starting back): head for its place from where they are
        wk.path = route([wk.f.root.position.x, wk.f.root.position.z], s.p)
        wk.phase = 'to'
        wk.target = s.p
        wk.taskId = t.id
      }
      wk.yaw = s.yaw
      wk.key = `${t.status}|${t.id}`
      wk.rush = REPAIR_KINDS.has(t.kind)
      ;(wk.task as THREE.Mesh<THREE.BufferGeometry, THREE.MeshBasicMaterial>).material.color.set(t.status === '執行中' ? '#76b900' : '#f5b83d')
      if (Math.abs(wk.progress - t.progress) > 0.01) {
        wk.progress = t.progress
        wk.ring.geometry.dispose()
        wk.ring.geometry = ringGeo(t.progress)
      }
    })
    // task done (or reassigned away): walk back to standby, then join the staff corner
    for (const [id, wk] of walkers.current) {
      if (active.has(id) || wk.phase === 'back') continue
      wk.phase = 'back'
      wk.key = 'back'
      wk.path = route([wk.f.root.position.x, wk.f.root.position.z], STANDBY)
      wk.task.visible = false
      wk.ring.visible = false
    }
  }, [world.people.workers, world.tasks, group])
  // test hook (headless e2e only: dev build or ?test=1): the task walkers (nobody else leaves standby)
  useEffect(() => {
    if (!import.meta.env.DEV && !new URLSearchParams(location.search).has('test')) return
    const w = window as unknown as Record<string, unknown>
    w.__world3d = {
      walkers: () => [...walkers.current.entries()].map(([id, wk]) => ({
        id, shown: true, walking: wk.path.length > 0, x: wk.f.root.position.x, z: wk.f.root.position.z, key: wk.key,
        phase: wk.phase, task: wk.phase === 'back' ? null : wk.key.split('|')[0], task_id: wk.taskId,
        target: wk.target, progress: wk.progress,
      })),
    }
    return () => { delete w.__world3d }
  }, [])
  useFrame(({ clock, camera }, dt0) => {
    const dt = Math.min(dt0, 0.05)
    let i = 0
    for (const [id, wk] of walkers.current) {
      if (stepWalker(wk, clock.elapsedTime, dt, ++i, camera.quaternion)) { group.remove(wk.f.root); walkers.current.delete(id) }
    }
  })
  return <primitive object={group} />
}

/** The dock truck. Live: drives in when a truck is at the dock (crates in, tyres out), off when it leaves.
 *  Without a world it stays parked, as the diorama always had it. */
export function DockTruck({ trucks }: { trucks: WorldTruck[] | null }) {
  const built = useMemo(() => {
    const group = new THREE.Group()
    group.add(rbox(1.5, 0.95, 1.4, '#fffaf0', 0, -0.05, -0.05, 0.08), rbox(1.3, 0.62, 0.7, '#6fa8dc', 0, 0.05, 0.95, 0.12))
    group.add(rbox(1.0, 0.24, 0.05, '#cfe6ee', 0, 0.4, 1.3, 0.03))
    for (const [dx, dz] of [[-0.66, -0.4], [0.66, -0.4], [-0.66, 0.95], [0.66, 0.95]]) {
      const wh = cyl(0.17, 0.17, 0.12, '#3d342b', dx, -0.12, dz, 12); wh.rotation.z = Math.PI / 2; group.add(wh)
    }
    // open cargo on the back step: crates (delivery) or a stack of tyres (shipment)
    const cargoIn = new THREE.Group()
    for (let i = 0; i < 3; i++) cargoIn.add(box(0.3, 0.26, 0.3, i === 1 ? '#efe6d2' : '#c9a06a', -0.35 + i * 0.35, 0.03, -0.92))
    const cargoOut = new THREE.Group()
    for (let i = 0; i < 3; i++) cargoOut.add(cyl(0.17, 0.17, 0.1, '#2b2724', -0.35 + i * 0.35, 0.0, -0.92, 14))
    group.add(cargoIn, cargoOut)
    return { group, cargoIn, cargoOut }
  }, [])
  const live = useRef({ ...built, z: trucks === null ? TRUCK_PARK[1] : TRUCK_AWAY + DOCK.z1, target: TRUCK_PARK[1] })
  const here = trucks === null ? null : trucks[0] ?? null
  useEffect(() => {
    const s = live.current
    s.target = trucks === null || here ? TRUCK_PARK[1] : DOCK.z1 + TRUCK_AWAY
    s.cargoIn.visible = !!here && here.kind === 'inbound'
    s.cargoOut.visible = !!here && here.kind === 'outbound'
  }, [trucks, here])
  useFrame((_, dt0) => {
    const s = live.current
    const dz = s.target - s.z
    if (Math.abs(dz) > 0.01) s.z += Math.sign(dz) * Math.min(Math.abs(dz), 5 * Math.min(dt0, 0.25))   // ~2.5 s in or out
    s.group.position.set(TRUCK_PARK[0], 0, s.z)
    s.group.visible = s.z < DOCK.z1 + TRUCK_AWAY - 0.5
  })
  useEffect(() => {
    if (!import.meta.env.DEV && !new URLSearchParams(location.search).has('test')) return
    const w = window as unknown as Record<string, unknown>
    w.__truck3d = () => ({ z: live.current.z, visible: live.current.group.visible, parkedZ: TRUCK_PARK[1],
      cargo: live.current.cargoIn.visible ? 'inbound' : live.current.cargoOut.visible ? 'outbound' : null })
    return () => { delete w.__truck3d }
  }, [])
  return <primitive object={built.group} />
}
