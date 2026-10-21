// 「請 AI 重排」 on a ② 交期風險 item (CJ, 2026-10-20: 「中間計劃下面只有請 AI 重排問 AI 為什麼沒有核准」): there is no
// approval card until the agent has planned, so the item tracks its replan — sending → AI 規劃中… (the event the
// command opened) → the card arrives and the home screen selects it. Kept outside React so it survives tab switches.
import { useSyncExternalStore } from 'react'

export interface Replan { orderId: string; state: 'sending' | 'planning' | 'ready' | 'closed' | 'failed'; eventId: string | null; error: string | null; since: number
  /** closed: the task ended without an approval card (e.g. nothing to change); its outcome */
  outcome?: string | null
}

/** The one line a ② item shows before anyone asked the AI. */
export const REPLAN_HINT = 'NemoClaw 會擬計劃；好了這裡會出現「核准並派發」'

let replans: Record<string, Replan> = {}
const subs = new Set<() => void>()
const emit = () => { for (const f of subs) f() }

export function setReplan(orderId: string, patch: Partial<Replan>) {
  const cur = replans[orderId] ?? { orderId, state: 'sending', eventId: null, error: null, since: Date.now() }
  replans = { ...replans, [orderId]: { ...cur, ...patch } }
  emit()
}

export function useReplans(): Record<string, Replan> {
  return useSyncExternalStore((f) => { subs.add(f); return () => { subs.delete(f) } }, () => replans)
}
