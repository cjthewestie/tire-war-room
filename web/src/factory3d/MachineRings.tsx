// Progress rings above EVERY machine (issue #36, CJ 2026-10-20 「其他機台為什麼沒有弄頭上的進度圈圈」). GoodsRings shows
// the tyres at a machine; this shows what the machine itself is doing, from data the server already sends
// (machineRing.ts): a worker task there, maintenance, an alarm, its running batch / cure, or a small dim idle dot.
// A machine that already carries goods rings (硫化中 / 備料中 / 生產中) adds only a task / maintenance / alarm ring,
// stacked above them.
import { useFrame } from '@react-three/fiber'
import { useEffect, useMemo, useRef } from 'react'
import * as THREE from 'three'
import type { MachineTile, WorldSnapshot } from '../contracts'
import { machineRing, type MachineRing, type RingRecovery } from './machineRing'
import { LAMP_POST, PLACED, type Placed } from './layout'

const R_IN = 0.12, R_OUT = 0.18, SEG = 64   // a little larger than a goods ring: readable from the overview too
const GOODS_BASE = 0.55, GOODS_STEP = 0.36, MAX_GOODS = 3     // GoodsRings' own stack

const NO_RECS: Record<string, RingRecovery> = {}

interface Slot { p: Placed; grp: THREE.Group; bg: THREE.Mesh; fg: THREE.Mesh; dot: THREE.Mesh }
interface Shown { id: string; ring: MachineRing; visible: boolean; y: number }

function makeSlot(p: Placed): Slot {
  const grp = new THREE.Group()
  const bg = new THREE.Mesh(new THREE.RingGeometry(R_IN, R_OUT, SEG), new THREE.MeshBasicMaterial({ color: '#2b2f36', side: THREE.DoubleSide, transparent: true, opacity: 0.85 }))
  // a full circle clockwise from 12 o'clock; the progress is how many of its segments are drawn (index draw range)
  const fg = new THREE.Mesh(new THREE.RingGeometry(R_IN, R_OUT, SEG, 1, Math.PI / 2, -Math.PI * 2), new THREE.MeshBasicMaterial({ side: THREE.DoubleSide }))
  fg.position.z = 0.002
  const dot = new THREE.Mesh(new THREE.CircleGeometry(R_IN * 0.5, 16), new THREE.MeshBasicMaterial({ side: THREE.DoubleSide, transparent: true }))
  grp.add(bg, fg, dot)
  grp.visible = false
  return { p, grp, bg, fg, dot }
}

function paint(s: Slot, x: Shown) {
  s.grp.visible = x.visible
  if (!x.visible) return
  const r = x.ring
  s.grp.position.set(s.p.x, x.y, s.p.z)
  const idle = r.kind === 'idle'
  s.bg.visible = !idle
  s.fg.visible = !idle && r.progress > 0
  s.fg.geometry.setDrawRange(0, Math.max(1, Math.round(r.progress * SEG)) * 6)
  ;(s.fg.material as THREE.MeshBasicMaterial).color.set(r.color)
  const dm = s.dot.material as THREE.MeshBasicMaterial
  dm.color.set(r.color)
  dm.opacity = idle ? 0.45 : 1
  s.dot.scale.setScalar(idle ? 0.7 : 1)
}

/** `now` (factory ms, from the clock: steadier than the world's 10-second steps), `speed` and `recs` (回溫中 / 已恢復 after a
 *  fast repair, recovery.ts) feed machineRing. */
export function MachineRings({ tiles, world, now: clockNow, speed = 1, recs = NO_RECS }: { tiles: Record<string, MachineTile>; world: WorldSnapshot | null; now?: number; speed?: number; recs?: Record<string, RingRecovery> }) {
  // three.js objects live in a ref and are changed only in the frame loop
  const slots = useRef<Slot[] | null>(null)
  const group = useRef<THREE.Group>(null)
  const now = clockNow || (world ? Date.parse(world.factory_time) : 0)
  const goodsAt = useMemo(() => {
    const n: Record<string, number> = {}
    for (const g of world?.goods?.items ?? []) if (g.machine_id) n[g.machine_id] = (n[g.machine_id] ?? 0) + 1
    return n
  }, [world?.goods])
  const shown = useMemo<Shown[]>(() => PLACED.map((p) => {
    const ring = machineRing(p.id, tiles[p.id], world, now, speed, recs[p.id] ?? null)
    const k = Math.min(goodsAt[p.id] ?? 0, MAX_GOODS)
    const visible = k === 0 || ring.kind === 'task' || ring.kind === 'recover' || ring.kind === 'maint' || ring.kind === 'alarm'
    return { id: p.id, ring, visible, y: k > 0 ? p.h + GOODS_BASE + k * GOODS_STEP : p.h + LAMP_POST + 0.5 }   // clear of the status lamp and its glow
  }), [tiles, world, now, speed, goodsAt, recs])
  const latest = useRef(shown)
  const dirty = useRef(true)
  useEffect(() => { latest.current = shown; dirty.current = true }, [shown])
  useEffect(() => () => {
    for (const s of slots.current ?? []) s.grp.traverse((o) => { const me = o as THREE.Mesh; if (me.isMesh) { me.geometry.dispose(); (me.material as THREE.Material).dispose() } })
  }, [])
  useFrame(({ camera }) => {
    if (!group.current) return
    if (!slots.current) {
      slots.current = PLACED.map(makeSlot)
      for (const s of slots.current) group.current.add(s.grp)
    }
    if (dirty.current) {
      dirty.current = false
      latest.current.forEach((x, i) => paint(slots.current![i], x))
    }
    for (const s of slots.current) if (s.grp.visible) s.grp.quaternion.copy(camera.quaternion)
  })
  // test hook (headless e2e only: dev build or ?test=1)
  useEffect(() => {
    if (!import.meta.env.DEV && !new URLSearchParams(location.search).has('test')) return
    const w = window as unknown as Record<string, unknown>
    w.__rings3d = () => shown.map((x) => ({ machine: x.id, kind: x.ring.kind, progress: x.ring.progress, label: x.ring.label, visible: x.visible, y: x.y }))
    return () => { delete w.__rings3d }
  }, [shown])
  return <group ref={group} />
}
