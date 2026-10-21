// 「請 AI 重排」 on a ② 交期風險 item (CJ, 2026-10-20: 「中間計劃下面只有請 AI 重排問 AI 為什麼沒有核准」): there is no
// approval card until the agent has planned, so the item tracks its replan — sending → AI 規劃中… (the event the
// command opened) → the card arrives and the home screen selects it. Kept outside React so it survives tab switches.
import { useSyncExternalStore } from 'react'

export interface Replan { orderId: string; state: 'sending' | 'planning' | 'ready' | 'closed' | 'failed'; eventId: string | null; error: string | null; since: number
  /** closed: the task ended without an approval card (e.g. nothing to change); its outcome */
  outcome?: string | null
}

/** The one line a ② item shows before anyone asked the AI. */
// CJ 2026-10-21: 「流程應該改成有事項需要計劃的時候要已經計劃好了等我點下面的核准或是討論」: the backend drafts it already
export const REPLAN_HINT = 'Agent Nemo 正在預先擬計畫，好了這裡會出現核准卡'

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
