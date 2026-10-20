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
  /** the supervisor's choice: on by default (CJ, 2026-10-20), remembered on this phone */
  enabled: boolean
  /** sound unlocked by a tap (browsers allow audio and the permission prompt only from a user gesture) */
  armed: boolean
  permission: AlertPermission
  /** the browser refused: notifications denied, or no sound after the tap */
  blocked: string | null
  held: number
  lastAlert: string | null
  arm(): Promise<void>
  setEnabled(on: boolean): void
}

const PREF_KEY = 'warroom.phone.alerts'
export function readAlertPref(): boolean {
  try { return localStorage.getItem(PREF_KEY) !== 'off' } catch { return true }
}
function writeAlertPref(on: boolean): void {
  try { localStorage.setItem(PREF_KEY, on ? 'on' : 'off') } catch { /* private mode: the choice lasts this visit */ }
}

/** Watches the pending list; fires push alerts on new requests, holds them while offline. */
export function useApprovalAlerts(pending: ApprovalRequest[], online: boolean, env: PushEnv = browserEnv()): ApprovalAlerts {
  const st = useRef<PushState>({ seen: new Set(), held: [] })
  const envRef = useRef(env)
  useEffect(() => { envRef.current = env })
  const baseTitle = useRef(typeof document !== 'undefined' ? document.title : '')
  const [enabled, setEnabledState] = useState(readAlertPref)
  const [armed, setArmed] = useState(false)
  const [blocked, setBlocked] = useState<string | null>(null)
  const [tries, setTries] = useState(0)          // a tap that did not unlock sound listens for the next tap
  const [permission, setPermission] = useState<AlertPermission>(currentPermission)
  const enabledRef = useRef(enabled)
  useEffect(() => { enabledRef.current = enabled }, [enabled])
  const [held, setHeld] = useState(0)
  const [lastAlert, setLastAlert] = useState<string | null>(null)

  useEffect(() => {
    const { fire, flushed } = diffPending(st.current, pending, online)
    setHeld(st.current.held.length)
    const e = envRef.current
    e.setTitle(pending.length ? `(${pending.length}) 待核准・${baseTitle.current}` : baseTitle.current)
    if (!fire.length || !enabledRef.current) return
    const { title, body } = alertText(fire, flushed)
    e.notify(title, body, `approval-${fire[0].id}`)
    e.vibrate([300, 120, 300, 120, 600])
    e.beep()
    setLastAlert(title)
  }, [pending, online])

  // Re-ring every 60 s while something is still waiting, so a missed first alert is not the only one.
  useEffect(() => {
    if (!pending.length || !online || !enabled) return
    const t = window.setInterval(() => { envRef.current.vibrate([300]); envRef.current.beep() }, 60_000)
    return () => window.clearInterval(t)
  }, [pending.length, online, enabled])

  const arm = useCallback(async () => {
    // Browsers only allow sound after a tap, and permission prompts only from a tap.
    let sound = false
    try {
      const AC = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
      if (AC) { audio = audio ?? new AC(); await audio.resume(); sound = audio.state === 'running' }
    } catch { /* no audio on this device */ }
    let perm = currentPermission()
    if (perm === 'default') {
      try { perm = (await Notification.requestPermission()) as AlertPermission } catch { /* ignore */ }
    }
    setPermission(perm)
    setBlocked(perm === 'denied' ? '瀏覽器已封鎖通知：請到瀏覽器的網站設定允許通知（聲音與震動仍會提醒）'
      : !sound ? '瀏覽器未允許播放聲音：再點一下畫面試試，或檢查手機靜音' : null)
    setArmed(sound)
    setTries((n) => n + 1)
    envRef.current.beep()
  }, [])

  // On by default: the first tap anywhere on the page unlocks sound and asks for notification permission.
  useEffect(() => {
    if (!enabled || armed || typeof document === 'undefined') return
    const onTap = () => { void arm() }
    document.addEventListener('pointerdown', onTap, { capture: true, once: true })
    return () => document.removeEventListener('pointerdown', onTap, { capture: true })
  }, [enabled, armed, arm, tries])

  const setEnabled = useCallback((on: boolean) => {
    writeAlertPref(on)
    setEnabledState(on)
    if (!on) setBlocked(null)
  }, [])

  return { enabled, armed, permission, blocked, held, lastAlert, arm, setEnabled }
}
