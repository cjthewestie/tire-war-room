// The pending-approval alert every war-room page carries (PendingApprovalAlert): count, the oldest card, how long it
// has waited and how long until it times out. CJ, 2026-10-22: 「核准區有代辦時應該全部頁面都要看得到告警」.
// Pure: no React, no DOM, so e2e/approval-alert-unit.mjs checks it without a browser.
import type { ApprovalRequest } from './contracts'
import { ms } from './format'
import { plain } from './plain'

/** Below this many wall seconds left, the bar turns red and pulses (the timeout is 10 min, approval_timeout_s). */
export const URGENT_S = 120

export interface AlertModel {
  n: number
  oldest: ApprovalRequest
  title: string
  waitedS: number            // wall seconds the oldest card has waited
  leftS: number | null       // wall seconds until it times out; null when the card has no expires_at
  red: boolean               // < 2 min left, or any R3 card waiting
  pulse: boolean             // < 2 min left only
}

/** The oldest pending card (created first): the one the supervisor should clear next. */
export function oldestPending(pending: ApprovalRequest[]): ApprovalRequest | null {
  let best: ApprovalRequest | null = null
  for (const p of pending) if (p.status === 'pending' && (!best || ms(p.created_at) < ms(best.created_at))) best = p
  return best
}

/** expires_at and created_at are FACTORY time (state_machine._approval_expires_at scales the 10 min by the sim
 *  speed), so a factory-time gap divided by the speed is the wall time the supervisor actually has. */
export function alertModel(pending: ApprovalRequest[], nowFactory: number, speed = 1): AlertModel | null {
  const oldest = oldestPending(pending)
  if (!oldest) return null
  const now = Number.isFinite(nowFactory) ? nowFactory : ms(oldest.created_at)   // no clock yet: show it, times from 0
  const k = speed > 0 ? speed : 1
  const waitedS = Math.max(0, (now - ms(oldest.created_at)) / 1000 / k)
  const exp = ms(oldest.expires_at)
  const leftS = Number.isFinite(exp) ? Math.max(0, (exp - now) / 1000 / k) : null
  const pulse = leftS != null && leftS < URGENT_S
  const live = pending.filter((p) => p.status === 'pending')
  return { n: live.length, oldest, title: plain(oldest.title), waitedS, leftS, pulse, red: pulse || live.some((p) => p.risk === 'R3') }
}

/** 135 → "2:15"; 4000 → "66:40" */
export function fmtClock(s: number): string {
  const t = Math.max(0, Math.floor(s))
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`
}

/** "已等 2 分" / "已等 45 秒" */
export function fmtWaited(s: number): string {
  return s < 60 ? `已等 ${Math.floor(s)} 秒` : `已等 ${Math.floor(s / 60)} 分`
}

/** The browser-tab title while cards wait: 「(N) 待核准・<page title>」 */
export function titleWithPending(base: string, n: number): string {
  return n > 0 ? `(${n}) 待核准・${base}` : base
}

/** 主控台 with that event selected: `/#approve?event=<id>` (App.tsx reads it). `search` keeps ?mockch / ?live. */
export function approveHref(eventId: string, search = ''): string {
  return `/${search}#approve?event=${encodeURIComponent(eventId)}`
}
