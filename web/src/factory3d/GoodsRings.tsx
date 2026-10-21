// Goods status rings above the machines (issue #17): every tyre the world reports at a machine (硫化中 → its press,
// 備料中 → A 線密煉機, 生產中 → 貼合) gets a small billboard progress ring above that machine, coloured by status;
// several on one machine stack upwards (max 3 shown). Data: GET /api/world goods (warroom/sim/work.py).
// Fixed slots like MachineRings: made once and reused, a progress change only moves the index draw range (rebuilding
// every ring on each 1 % of progress churned the GC at 10× with concurrent_3).
import { useFrame } from '@react-three/fiber'
import { useEffect, useMemo, useRef } from 'react'
import * as THREE from 'three'
import type { GoodsStatus, GoodsView } from '../contracts'
import { PLACED_BY_ID } from './layout'

const COLOR: Record<GoodsStatus, string> = {
  待補料: '#9aa0a8', 備料中: '#f5b83d', 生產中: '#f5b83d', 硫化中: '#76b900', 完成: '#4fb3d9', 待出貨: '#4fb3d9', 已出貨: '#4fb3d9',
}
const MAX_STACK = 3
const R_IN = 0.1, R_OUT = 0.15, STEP_Y = 0.36, SEG = 32

interface Ring { mid: string; status: GoodsStatus; progress: number; order: string; color: string; dot: string; at: [number, number, number] }
interface Slot { grp: THREE.Group; fg: THREE.Mesh; dot: THREE.Mesh }

function makeSlot(): Slot {
  const grp = new THREE.Group()
  const bg = new THREE.Mesh(new THREE.RingGeometry(R_IN, R_OUT, SEG), new THREE.MeshBasicMaterial({ color: '#2b2f36', side: THREE.DoubleSide, transparent: true, opacity: 0.85 }))
  // a full circle clockwise from 12 o'clock; the progress is how many of its segments are drawn (index draw range)
  const fg = new THREE.Mesh(new THREE.RingGeometry(R_IN, R_OUT, SEG, 1, Math.PI / 2, -Math.PI * 2), new THREE.MeshBasicMaterial({ side: THREE.DoubleSide }))
  fg.position.z = 0.002
  const dot = new THREE.Mesh(new THREE.CircleGeometry(R_IN * 0.55, 16), new THREE.MeshBasicMaterial({ side: THREE.DoubleSide }))
  grp.add(bg, fg, dot)
  grp.visible = false
  return { grp, fg, dot }
}

function paint(s: Slot, r: Ring | undefined) {
  s.grp.visible = !!r
  if (!r) return
  s.grp.position.set(...r.at)
  s.fg.geometry.setDrawRange(0, Math.max(1, Math.round(Math.min(1, r.progress) * SEG)) * 6)
  ;(s.fg.material as THREE.MeshBasicMaterial).color.set(r.color)
  ;(s.dot.material as THREE.MeshBasicMaterial).color.set(r.dot)
}

export function GoodsRings({ goods }: { goods: GoodsView }) {
  // three.js objects live in a ref and are changed only in the frame loop
  const slots = useRef<Slot[]>([])
  const group = useRef<THREE.Group>(null)
  const rings = useMemo<Ring[]>(() => {
    const out: Ring[] = []
    const per: Record<string, number> = {}
    for (const g of goods.items) {
      const m = g.machine_id ? PLACED_BY_ID[g.machine_id] : undefined
      if (!m || !g.machine_id) continue
      const k = per[g.machine_id] ?? 0
      per[g.machine_id] = k + 1
      if (k >= MAX_STACK) continue
      out.push({ mid: g.machine_id, status: g.status, progress: g.progress, order: g.order_id, color: g.late ? '#ff5d52' : COLOR[g.status],
        dot: g.rush ? '#ff5d52' : COLOR[g.status], at: [m.x, m.h + 0.55 + k * STEP_Y, m.z] })
    }
    return out
  }, [goods.items])
  const latest = useRef(rings)
  const dirty = useRef(true)
  useEffect(() => { latest.current = rings; dirty.current = true }, [rings])
  useEffect(() => () => {
    for (const s of slots.current) s.grp.traverse((o) => { const me = o as THREE.Mesh; if (me.isMesh) { me.geometry.dispose(); (me.material as THREE.Material).dispose() } })
  }, [])
  useFrame(({ camera }) => {
    if (!group.current) return
    if (dirty.current) {
      dirty.current = false
      // a slot is added only when more rings show than ever before; after that they are only repainted
      while (slots.current.length < latest.current.length) { const s = makeSlot(); slots.current.push(s); group.current.add(s.grp) }
      slots.current.forEach((s, i) => paint(s, latest.current[i]))
    }
    for (const s of slots.current) if (s.grp.visible) s.grp.quaternion.copy(camera.quaternion)
  })
  // test hook (headless e2e only: dev build or ?test=1)
  useEffect(() => {
    if (!import.meta.env.DEV && !new URLSearchParams(location.search).has('test')) return
    const w = window as unknown as Record<string, unknown>
    w.__goods3d = () => rings.map((r) => ({ machine: r.mid, status: r.status, progress: r.progress, order: r.order, visible: true }))
    return () => { delete w.__goods3d }
  }, [rings])
  return <group ref={group} />
}
