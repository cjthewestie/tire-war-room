// Goods status rings above the machines (issue #17): every tyre the world reports at a machine (硫化中 → its press,
// 備料中 → A 線密煉機, 生產中 → 貼合) gets a small billboard progress ring above that machine, coloured by status;
// several on one machine stack upwards (max 3 shown). Data: GET /api/world goods (warroom/sim/work.py).
import { useFrame } from '@react-three/fiber'
import { useEffect, useMemo } from 'react'
import * as THREE from 'three'
import type { GoodsItem, GoodsStatus, GoodsView } from '../contracts'
import { PLACED_BY_ID } from './layout'

const COLOR: Record<GoodsStatus, string> = {
  待補料: '#9aa0a8', 備料中: '#f5b83d', 生產中: '#f5b83d', 硫化中: '#76b900', 完成: '#4fb3d9', 待出貨: '#4fb3d9', 已出貨: '#4fb3d9',
}
const MAX_STACK = 3
const R_IN = 0.1, R_OUT = 0.15, STEP_Y = 0.36

interface Ring { mid: string; status: GoodsStatus; progress: number; order: string; obj: THREE.Group }

function ringObj(g: GoodsItem): THREE.Group {
  const grp = new THREE.Group()
  const bg = new THREE.Mesh(new THREE.RingGeometry(R_IN, R_OUT, 32), new THREE.MeshBasicMaterial({ color: '#2b2f36', side: THREE.DoubleSide, transparent: true, opacity: 0.85 }))
  const p = Math.max(0.02, Math.min(1, g.progress))
  const fg = new THREE.Mesh(new THREE.RingGeometry(R_IN, R_OUT, 32, 1, Math.PI / 2, -p * Math.PI * 2),
    new THREE.MeshBasicMaterial({ color: g.late ? '#ff5d52' : COLOR[g.status], side: THREE.DoubleSide }))
  fg.position.z = 0.002
  const dot = new THREE.Mesh(new THREE.CircleGeometry(R_IN * 0.55, 16), new THREE.MeshBasicMaterial({ color: g.rush ? '#ff5d52' : COLOR[g.status], side: THREE.DoubleSide }))
  grp.add(bg, fg, dot)
  return grp
}

export function GoodsRings({ goods }: { goods: GoodsView }) {
  const group = useMemo(() => new THREE.Group(), [])
  const sig = goods.items.map((g) => `${g.id}:${g.machine_id}:${g.status}:${g.progress.toFixed(2)}:${g.late}`).join('|')
  const rings = useMemo<Ring[]>(() => {
    const out: Ring[] = []
    const per: Record<string, number> = {}
    for (const g of goods.items) {
      const m = g.machine_id ? PLACED_BY_ID[g.machine_id] : undefined
      if (!m || !g.machine_id) continue
      const k = per[g.machine_id] ?? 0
      per[g.machine_id] = k + 1
      if (k >= MAX_STACK) continue
      const obj = ringObj(g)
      obj.position.set(m.x, m.h + 0.55 + k * STEP_Y, m.z)
      out.push({ mid: g.machine_id, status: g.status, progress: g.progress, order: g.order_id, obj })
    }
    return out
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig])
  useEffect(() => {
    for (const r of rings) group.add(r.obj)
    return () => {
      for (const r of rings) {
        group.remove(r.obj)
        r.obj.traverse((o) => { const me = o as THREE.Mesh; if (me.isMesh) { me.geometry.dispose(); (me.material as THREE.Material).dispose() } })
      }
    }
  }, [rings, group])
  useFrame(({ camera }) => { for (const r of rings) r.obj.quaternion.copy(camera.quaternion) })
  // test hook (headless e2e only: dev build or ?test=1)
  useEffect(() => {
    if (!import.meta.env.DEV && !new URLSearchParams(location.search).has('test')) return
    const w = window as unknown as Record<string, unknown>
    w.__goods3d = () => rings.map((r) => ({ machine: r.mid, status: r.status, progress: r.progress, order: r.order, visible: r.obj.visible }))
    return () => { delete w.__goods3d }
  }, [rings])
  return <primitive object={group} />
}
