// Warning lights you cannot miss (CJ, 2026-10-22: 「3d戰情室的那個異常警示燈太不明顯了」), from any angle including the
// wide shot: a three-segment stack light on every machine (red / amber / green, the standard factory tower light), and
// for an abnormal machine a rotating beacon, a translucent light pillar rising ~3.6 above it, a glowing ring on the
// floor, and a floating label that always faces the camera at a readable size. Off screen, an arrow at the screen edge
// points to it (AlarmArrows → the DOM arrows FactoryApp draws). Severity per alarm.ts. One shared material per colour
// and state, nothing casts a shadow; reduced motion keeps the colours and slows the pulse.
import { Html } from '@react-three/drei'
import { useFrame, useThree } from '@react-three/fiber'
import { useEffect, useMemo, useRef } from 'react'
import * as THREE from 'three'
import { ALARM_HEX, arrowEls, pulseRate, resolvedNow, type Alarm, type AlarmLevel } from './alarm'
import { cyl } from './kit'
import { PLACED, PLACED_BY_ID, type Placed } from './layout'

const REDUCED = typeof matchMedia !== 'undefined' && matchMedia('(prefers-reduced-motion: reduce)').matches
const RATE = pulseRate(REDUCED)
const FLASH_MS = 2500
const PILLAR_H = 3.6

const lit = (hex: string, i = 2.2) => new THREE.MeshStandardMaterial({ color: hex, emissive: hex, emissiveIntensity: i, roughness: 0.4, flatShading: true })
const dim = (hex: string) => new THREE.MeshStandardMaterial({ color: new THREE.Color(hex).lerp(new THREE.Color('#6b6f75'), 0.7).multiplyScalar(0.55), roughness: 0.6, flatShading: true })
const glow = (hex: string, opacity: number) => new THREE.MeshBasicMaterial({ color: hex, transparent: true, opacity, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide, toneMapped: false })

/** Shared materials: segments lit / dim per colour, the approval blink, the resolved flash; pillar, beam and halo per level. */
function materials() {
  return {
    redLit: lit(ALARM_HEX.stop), redDim: dim(ALARM_HEX.stop), amberLit: lit(ALARM_HEX.anomaly), amberDim: dim(ALARM_HEX.anomaly),
    greenOff: dim(ALARM_HEX.resolved), greenDim: lit(ALARM_HEX.resolved, 0.35), amberBlink: lit(ALARM_HEX.anomaly), greenFlash: lit(ALARM_HEX.resolved, 2.4),
    pillar: { anomaly: glow(ALARM_HEX.anomaly, 0.22), approval: glow(ALARM_HEX.anomaly, 0.22), stop: glow(ALARM_HEX.stop, 0.26) } as Record<AlarmLevel, THREE.MeshBasicMaterial>,
    beam: { anomaly: glow(ALARM_HEX.anomaly, 0.28), approval: glow(ALARM_HEX.anomaly, 0.28), stop: glow(ALARM_HEX.stop, 0.32) } as Record<AlarmLevel, THREE.MeshBasicMaterial>,
    halo: { anomaly: glow(ALARM_HEX.anomaly, 0.5), approval: glow(ALARM_HEX.anomaly, 0.5), stop: glow(ALARM_HEX.stop, 0.55) } as Record<AlarmLevel, THREE.MeshBasicMaterial>,
  }
}
type Mats = ReturnType<typeof materials>
let MATS: Mats | null = null
const mats = () => (MATS ??= materials())

/** Where the stack light stands: the machine's aisle-side corner, on top of it. */
function stackSpot(p: Placed): [number, number, number] {
  const front = p.line === 'CURE' ? -1 : 1
  return [p.x + p.w / 2 - 0.07, p.h, p.z + front * (p.d / 2 - 0.07)]
}

/** The tower light on every machine. */
function StackLights({ alarms, flash }: { alarms: Record<string, Alarm>; flash: { current: Record<string, number> } }) {
  const m = mats()
  const { group, segs } = useMemo(() => {
    const group = new THREE.Group()
    const segs: Record<string, THREE.Mesh[]> = {}
    const segGeo = new THREE.CylinderGeometry(0.055, 0.055, 0.085, 12)
    for (const p of PLACED) {
      const [x, y, z] = stackSpot(p)
      const pole = cyl(0.014, 0.014, 0.2, '#5d636b', x, y + 0.1, z, 6)
      pole.castShadow = false
      group.add(pole)
      const s = [m.greenDim, m.amberDim, m.redDim].map((mat, i) => {
        const me = new THREE.Mesh(segGeo, mat)
        me.position.set(x, y + 0.25 + i * 0.09, z)
        group.add(me)
        return me
      })
      group.add(Object.assign(cyl(0.06, 0.06, 0.02, '#3b4048', x, y + 0.385, z, 12), { castShadow: false }))
      segs[p.id] = s
    }
    return { group, segs }
  }, [m])
  const ref = useRef(alarms)
  useEffect(() => { ref.current = alarms }, [alarms])
  useFrame(({ clock }) => {
    const t = clock.elapsedTime * RATE
    const now = performance.now()
    // approval: amber on / off at ~1 Hz, tinted toward blue on the off beat (the war room's blue-amber)
    const on = Math.sin(t * Math.PI * 2) > 0
    m.amberBlink.emissive.set(on ? ALARM_HEX.anomaly : ALARM_HEX.blue)
    m.amberBlink.emissiveIntensity = on ? 2.4 : 0.9
    m.greenFlash.emissiveIntensity = 1.2 + Math.abs(Math.sin(t * 8)) * 2
    const a = ref.current, f = flash.current
    for (const id in segs) {
      const [g, am, r] = segs[id]
      const lv = a[id]?.level
      const flashing = (f[id] ?? 0) > now
      // normal: green on, dimmed; abnormal: green off and the alarm colour lit; just resolved: green flash
      g.material = flashing ? m.greenFlash : lv ? m.greenOff : m.greenDim
      am.material = lv === 'approval' ? m.amberBlink : lv === 'anomaly' ? m.amberLit : m.amberDim
      r.material = lv === 'stop' ? m.redLit : m.redDim
    }
  })
  return <primitive object={group} />
}

/** Beacon, light pillar and floor halo for one abnormal machine. */
function Beacon({ p, level }: { p: Placed; level: AlarmLevel }) {
  const m = mats()
  const { group, spin, halo } = useMemo(() => {
    const group = new THREE.Group()
    const top = p.h + 0.62
    const pg = new THREE.CylinderGeometry(0.2, 0.34, PILLAR_H, 18, 1, true)
    pg.translate(0, PILLAR_H / 2, 0)
    const pillar = new THREE.Mesh(pg, m.pillar[level])
    pillar.position.set(p.x, top, p.z)
    pillar.renderOrder = 5
    group.add(pillar)
    // the rotating beacon: a dome and two light blades sweeping round
    const spin = new THREE.Group()
    spin.position.set(p.x, top, p.z)
    const dome = new THREE.Mesh(new THREE.SphereGeometry(0.11, 12, 8, 0, Math.PI * 2, 0, Math.PI / 2), level === 'stop' ? m.redLit : m.amberLit)
    spin.add(dome)
    for (const s of [0, Math.PI]) {
      const bg = new THREE.ConeGeometry(0.16, 0.9, 12, 1, true)
      bg.translate(0, -0.45, 0)
      bg.rotateZ(Math.PI / 2)
      const blade = new THREE.Mesh(bg, m.beam[level])
      blade.rotation.y = s
      blade.position.y = 0.06
      blade.renderOrder = 5
      spin.add(blade)
    }
    group.add(spin)
    const r = Math.max(p.w, p.d) * 0.72
    const halo = new THREE.Mesh(new THREE.RingGeometry(r, r + 0.22, 40), m.halo[level])
    halo.rotation.x = -Math.PI / 2
    halo.position.set(p.x, 0.03, p.z)
    halo.renderOrder = 4
    group.add(halo)
    return { group, spin, halo }
  }, [p, level, m])
  useFrame(({ clock }) => {
    const t = clock.elapsedTime * RATE
    spin.rotation.y = t * 3.2
    const s = 1 + ((t * 0.7) % 1) * 0.35
    halo.scale.set(s, s, 1)
  })
  return <primitive object={group} />
}

/** Pulse the shared pillar / halo materials once per frame (not per machine). */
function Pulse() {
  const m = mats()
  useFrame(({ clock }) => {
    const t = clock.elapsedTime * RATE
    const k = 0.5 + 0.5 * Math.sin(t * 3)
    for (const lv of ['anomaly', 'approval', 'stop'] as AlarmLevel[]) {
      m.pillar[lv].opacity = (lv === 'stop' ? 0.2 : 0.15) + k * 0.16
      m.halo[lv].opacity = 0.35 + k * 0.35
    }
    const on = Math.sin(t * Math.PI * 2) > 0
    const c = on ? ALARM_HEX.anomaly : ALARM_HEX.blue
    m.pillar.approval.color.set(c); m.beam.approval.color.set(c); m.halo.approval.color.set(c)
  })
  return null
}

/** Screen-edge arrows for alarms out of view: positions written straight into the DOM arrows (no re-render). */
function AlarmArrows({ ids }: { ids: string[] }) {
  const { camera, size } = useThree()
  const v = useMemo(() => new THREE.Vector3(), [])
  useFrame(() => {
    for (const id of ids) {
      const el = arrowEls.get(id)
      const p = PLACED_BY_ID[id]
      if (!el || !p) continue
      v.set(p.x, p.h + 1, p.z).project(camera)
      const behind = v.z > 1
      let x = v.x, y = v.y
      if (behind) { x = -x; y = -y }
      const inside = !behind && Math.abs(x) < 0.92 && Math.abs(y) < 0.9
      if (inside) { el.style.display = 'none'; continue }
      // push to the edge along the direction from the screen centre
      const k = 0.9 / Math.max(Math.abs(x), Math.abs(y), 1e-6)
      x *= k; y *= k
      const px = (x + 1) / 2 * size.width, py = (1 - y) / 2 * size.height
      const ang = Math.atan2(-y, x)
      el.style.display = 'flex'
      el.style.transform = `translate(${Math.round(px)}px, ${Math.round(py)}px) translate(-50%, -50%)`
      el.style.setProperty('--ang', `${ang}rad`)
    }
  })
  return null
}

export function Alarms({ alarms, labels, arrows }: { alarms: Record<string, Alarm>; labels: boolean; arrows: boolean }) {
  // green flash when a machine stops being abnormal
  const prev = useRef<Record<string, Alarm>>({})
  const flash = useRef<Record<string, number>>({})
  useEffect(() => {
    for (const id of resolvedNow(prev.current, alarms)) flash.current[id] = performance.now() + FLASH_MS
    prev.current = alarms
  }, [alarms])
  const ids = Object.keys(alarms).filter((id) => PLACED_BY_ID[id])
  return (
    <>
      <StackLights alarms={alarms} flash={flash} />
      <Pulse />
      {ids.map((id) => <Beacon key={`${id}-${alarms[id].level}`} p={PLACED_BY_ID[id]} level={alarms[id].level} />)}
      {labels && ids.map((id) => {
        const p = PLACED_BY_ID[id]
        return (
          <Html key={id} position={[p.x, p.h + 2.0, p.z]} center zIndexRange={[8, 6]} style={{ pointerEvents: 'none' }}>
            <div className={`f3-alarm-tag ${alarms[id].level}`} data-testid={`f3-alarm-${id}`}>{alarms[id].text}</div>
          </Html>
        )
      })}
      {arrows && <AlarmArrows ids={ids} />}
    </>
  )
}
