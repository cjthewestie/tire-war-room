// Supervisor-phone push (page 5 「推播核准（廠內區網）」, page 7 「API：主管手機推播」, page 9 弱網「只延後同步與推播」).
// LAN-only, no cloud push service: a new pending approval rings (Web Audio), vibrates, flashes the tab title
// and, when the browser allows it, raises a system notification. While the link is down the alerts are held
// and flushed as one alert when the phone reconnects.
// Fast repair (Kyle 10-21 flow): 維修完成 (Event.repaired_at, the readings coming back) and 已恢復正常・異常報告已產生 (the
// event closed with its report written: Event.review pending and Event.ai_report) ring once each, as notices: nothing
// waits for them, so they do not re-ring or count as waiting in the tab title.
import { useCallback, useEffect, useRef, useState } from 'react'
import type { Event, MachineTile } from '../contracts'
import { CH_LABEL, expectationOf, normalOf, readingText, recoverText, reportReady } from '../aiStatus'
import { machineLabel } from '../factory'
import { fmtHM } from '../format'
import { plain } from '../plain'

export type AlertPermission = 'granted' | 'denied' | 'default' | 'unsupported'

export interface PushEnv {
  notify(title: string, body: string, tag: string): boolean
  vibrate(pattern: number[]): boolean
  beep(): boolean
  setTitle(t: string): void
}

/** What rings: a pending approval card (ApprovalRequest fits; its R0–R3 code stays off the supervisor's notice), or a
 *  fast-repair notice (notice set). */
export interface AlertItem { id: string; title: string; reason: string; notice?: 'repaired' | 'report' }

const NOTICE_HEAD: Record<NonNullable<AlertItem['notice']>, string> = { repaired: '維修完成', report: '已恢復正常，報告已產生' }

/** The fast-repair notices of these events, oldest repair first: 維修完成 while the readings come back (repaired, not
 *  closed), then 已恢復正常，報告已產生 once it closed with its report written and unread (review pending, ai_report; the
 *  draft can land after the close). `tiles` add the live temperature to the 維修完成 text. */
export function repairNotices(events: Event[] | undefined, tiles: MachineTile[] = []): AlertItem[] {
  const out: AlertItem[] = []
  for (const e of [...(events ?? [])].sort((a, b) => ((a.repaired_at ?? '') < (b.repaired_at ?? '') ? -1 : 1))) {
    if (!e.repaired_at) continue
    const title = machineLabel(e.machine_id)
    if (e.state !== 'closed' && e.state !== 'safe_stop') {
      const x = expectationOf(e)
      const now = x?.channel === 'temp_c' ? tiles.find((t) => t.machine_id === e.machine_id)?.temp_c ?? null : null
      out.push({ id: `repaired:${e.id}:${e.repaired_at}`, title, reason: recoverText(x, now, normalOf(e, x)), notice: 'repaired' })
    } else if (e.state === 'closed' && reportReady(e)) {
      out.push({ id: `report:${e.id}:${e.review.requested_at}`, title, reason: backText(e), notice: 'report' })
    }
  }
  return out
}

/** The report notice's text under its title (the machine): 「溫度回到 128°C（02:16 維修完成）」, the reading the ramp
 *  ended at (the anomaly's baseline, as the status line's 已恢復正常（128°C）); the outcome without 「已修復：」 when unknown. */
function backText(e: Event): string {
  const x = expectationOf(e)
  const normal = normalOf(e, x)
  if (!x || normal == null) return plain(e.outcome).replace(/^已修復[：:]\s*/, '') || '異常報告已產生'
  return `${CH_LABEL[x.channel]}回到 ${readingText(x.channel, normal)}${e.repaired_at ? `（${fmtHM(e.repaired_at)} 維修完成）` : ''}`
}

/** 「待核准」 or what the notices say, for the multi-item alert. */
export function waitLabel(items: AlertItem[]): string {
  const n = items.filter((a) => a.notice), q = items.length > n.length
  if (!n.length) return '待核准'
  if (q) return '待核准與通知'
  return n.every((a) => a.notice === 'repaired') ? '維修完成' : n.every((a) => a.notice === 'report') ? '報告已產生' : '維修通知'
}

/** Pure diff: which pending ids are new since last time, and which alerts to fire now. */
export interface PushState { seen: Set<string>; held: AlertItem[] }
export interface PushOut { fire: AlertItem[]; flushed: boolean }

export function diffPending(st: PushState, pending: AlertItem[], online: boolean): PushOut {
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

export function alertText(fire: AlertItem[], flushed: boolean): { title: string; body: string } {
  const first = fire[0]
  const title = fire.length === 1 ? `${first.notice ? NOTICE_HEAD[first.notice] : '待核准'}：${first.title}` : `${fire.length} 件${waitLabel(fire)}`
  // the card's reason in plain words (假設 H1 → 判斷一); no R0–R3 on a supervisor's screen
  const body = (flushed ? '（網路恢復後補推）' : '') + (fire.length === 1 ? plain(first.reason) : fire.map((a) => a.title).join('；'))
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

/** Watches the pending list (approval cards and fast-repair notices); fires push alerts on new ones, holds them while
 *  offline. Only approval cards count as waiting (tab title, the 60 s re-ring). */
export function useApprovalAlerts(pending: AlertItem[], online: boolean, env: PushEnv = browserEnv()): ApprovalAlerts {
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
    const asks = pending.filter((a) => !a.notice).length
    e.setTitle(asks ? `(${asks}) 待核准・${baseTitle.current}` : baseTitle.current)
    if (!fire.length || !enabledRef.current) return
    const { title, body } = alertText(fire, flushed)
    e.notify(title, body, `approval-${fire[0].id}`)
    e.vibrate([300, 120, 300, 120, 600])
    e.beep()
    setLastAlert(title)
  }, [pending, online])

  // Re-ring every 60 s while an approval is still waiting, so a missed first alert is not the only one.
  const waiting = pending.some((a) => !a.notice)
  useEffect(() => {
    if (!waiting || !online || !enabled) return
    const t = window.setInterval(() => { envRef.current.vibrate([300]); envRef.current.beep() }, 60_000)
    return () => window.clearInterval(t)
  }, [waiting, online, enabled])

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
    setBlocked(perm === 'denied' ? '系統通知被瀏覽器擋住，聲音和震動照常提醒'
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
