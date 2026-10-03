// Supervisor-phone push (page 5 「推播核准（廠內區網）」, page 7 「API：主管手機推播」, page 9 弱網「只延後同步與推播」).
// LAN-only, no cloud push service: a new pending approval rings (Web Audio), vibrates, flashes the tab title
// and, when the browser allows it, raises a system notification. While the link is down the alerts are held
// and flushed as one alert when the phone reconnects.
import { useCallback, useEffect, useRef, useState } from 'react'
import type { ApprovalRequest } from '../contracts'

export type AlertPermission = 'granted' | 'denied' | 'default' | 'unsupported'

export interface PushEnv {
  notify(title: string, body: string, tag: string): boolean
  vibrate(pattern: number[]): boolean
  beep(): boolean
  setTitle(t: string): void
}

/** Pure diff: which pending ids are new since last time, and which alerts to fire now. */
export interface PushState { seen: Set<string>; held: ApprovalRequest[] }
export interface PushOut { fire: ApprovalRequest[]; flushed: boolean }

export function diffPending(st: PushState, pending: ApprovalRequest[], online: boolean): PushOut {
  const fresh = pending.filter((a) => !st.seen.has(a.id))
  // Only ids still pending count as seen: a request that left the list (decided, or the scenario was reset)
  // and comes back is a new request and rings again. Held items decided elsewhere (touch screen) are dropped.
  const live = new Set(pending.map((a) => a.id))
  st.seen = live
  st.held = st.held.filter((a) => live.has(a.id))
  if (!online) { st.held.push(...fresh); return { fire: [], flushed: false } }
  const flushed = st.held.length > 0
  const fire = [...st.held, ...fresh]
  st.held = []
  return { fire, flushed }
}

export function alertText(fire: ApprovalRequest[], flushed: boolean): { title: string; body: string } {
  const first = fire[0]
  const title = fire.length === 1 ? `待核准：${first.title}` : `${fire.length} 件待核准`
  const body = (flushed ? '（網路恢復後補推）' : '') + (fire.length === 1 ? `${first.risk}・${first.reason}` : fire.map((a) => a.title).join('；'))
  return { title, body }
}

let audio: AudioContext | null = null
function browserEnv(): PushEnv {
  return {
    notify(title, body, tag) {
      try {
        if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return false
        new Notification(title, { body, tag, requireInteraction: true } as NotificationOptions)
        return true
      } catch { return false } // Android Chrome forbids page-context Notification; sound + vibrate still run.
    },
    vibrate(p) { try { return typeof navigator.vibrate === 'function' && navigator.vibrate(p) } catch { return false } },
    beep() {
      try {
        if (!audio || audio.state !== 'running') return false
        const t0 = audio.currentTime
        ;[0, 0.35].forEach((dt) => {
          const o = audio!.createOscillator(); const g = audio!.createGain()
          o.frequency.value = 880; o.connect(g); g.connect(audio!.destination)
          g.gain.setValueAtTime(0.0001, t0 + dt); g.gain.exponentialRampToValueAtTime(0.4, t0 + dt + 0.02)
          g.gain.exponentialRampToValueAtTime(0.0001, t0 + dt + 0.25)
          o.start(t0 + dt); o.stop(t0 + dt + 0.27)
        })
        return true
      } catch { return false }
    },
    setTitle(t) { document.title = t },
  }
}

function currentPermission(): AlertPermission {
  if (typeof Notification === 'undefined' || !window.isSecureContext) return 'unsupported'
  return Notification.permission as AlertPermission
}

export interface ApprovalAlerts {
  armed: boolean
  permission: AlertPermission
  held: number
  lastAlert: string | null
  arm(): Promise<void>
}

/** Watches the pending list; fires push alerts on new requests, holds them while offline. */
export function useApprovalAlerts(pending: ApprovalRequest[], online: boolean, env: PushEnv = browserEnv()): ApprovalAlerts {
  const st = useRef<PushState>({ seen: new Set(), held: [] })
  const envRef = useRef(env)
  useEffect(() => { envRef.current = env })
  const baseTitle = useRef(typeof document !== 'undefined' ? document.title : '')
  const [armed, setArmed] = useState(false)
  const [permission, setPermission] = useState<AlertPermission>(currentPermission)
  const [held, setHeld] = useState(0)
  const [lastAlert, setLastAlert] = useState<string | null>(null)

  useEffect(() => {
    const { fire, flushed } = diffPending(st.current, pending, online)
    setHeld(st.current.held.length)
    const e = envRef.current
    e.setTitle(pending.length ? `(${pending.length}) 待核准・${baseTitle.current}` : baseTitle.current)
    if (!fire.length) return
    const { title, body } = alertText(fire, flushed)
    e.notify(title, body, `approval-${fire[0].id}`)
    e.vibrate([300, 120, 300, 120, 600])
    e.beep()
    setLastAlert(title)
  }, [pending, online])

  // Re-ring every 60 s while something is still waiting, so a missed first alert is not the only one.
  useEffect(() => {
    if (!pending.length || !online) return
    const t = window.setInterval(() => { envRef.current.vibrate([300]); envRef.current.beep() }, 60_000)
    return () => window.clearInterval(t)
  }, [pending.length, online])

  const arm = useCallback(async () => {
    // Browsers only allow sound after a tap, and permission prompts only from a tap.
    try {
      const AC = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
      if (AC) { audio = audio ?? new AC(); await audio.resume() }
    } catch { /* no audio on this device */ }
    if (currentPermission() === 'default') {
      try { setPermission((await Notification.requestPermission()) as AlertPermission) } catch { /* ignore */ }
    }
    setArmed(true)
    envRef.current.beep()
  }, [])

  return { armed, permission, held, lastAlert, arm }
}
