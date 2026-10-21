// What the 狀態機 page shows about each run: its category (CJ, 2026-10-20: 機台異常 / 訂單類 / 人力類 / 早班開工 /
// 主管任務 / 問戰情室), the state it sits in, how long, which round, and what it waits on. Built from the live events
// plus GET /api/daylog/current (the audit log's state path with times).
import type { Event, EventState, WorkTask } from '../contracts'

export type Category = 'machine' | 'order' | 'people' | 'shift' | 'command' | 'ask'
export const CATEGORY_LABEL: Record<Category, string> = {
  machine: '機台異常', order: '訂單類', people: '人力類', shift: '早班開工', command: '主管任務', ask: '問戰情室',
}

export interface DayRun {
  id: string; type: 'event' | 'command' | 'plant' | 'ask'; machine_id: string | null; opened_at: string | null; closed_at: string | null
  path: { state: string; ts: string }[]; rounds: number; outcome: string; approvals: { decision: string; by: string; via: string | null; ts: string }[]
  pending_approvals: number; title: string | null; duration_min: number | null; by?: string
}
export interface Day {
  key: string; label: string; first: string; last: string; runs: DayRun[]
  summary: { runs: number; by_outcome: Record<string, number>; by_type: Record<string, number>; avg_close_min: number | null; approvals_decided: number; approvals_pending: number }
}

const ORDER_RE = /插單|急單|交期|訂單|缺料|原料|補料|重排|出貨/
const PEOPLE_RE = /缺人|請假|病假|人力|調人|加班/
const SHIFT_RE = /開工|早班/

export function categoryOf(ev: Event): Category {
  const text = `${ev.alert_card?.title ?? ''} ${ev.command ?? ''} ${ev.anomaly.message}`
  if (ev.source === 'supervisor') return 'command'
  if (ev.source === 'plant') return SHIFT_RE.test(text) ? 'shift' : PEOPLE_RE.test(text) ? 'people' : 'order'
  // a watcher event about materials or orders (e.g. a material watcher) rather than a machine reading
  if (!/^(?:[ABCD]-|BOND-|CURE-)/.test(ev.machine_id) && ORDER_RE.test(text)) return 'order'
  return 'machine'
}

export type WaitOn = '模型' | '工具' | '主管核准' | '人員任務' | '程式驗證' | '—'
export function waitingOn(ev: Event, tasks: WorkTask[]): WaitOn {
  const open = tasks.filter((t) => t.event_id === ev.id && t.status !== '完成').length
  switch (ev.state) {
    case 'detected': case 'classify': case 'plan': case 'replan': return '模型'
    case 'simulate': case 'gate': return '工具'
    case 'await_approval': return '主管核准'
    case 'execute': return open ? '人員任務' : '工具'
    case 'verify': return open ? '人員任務' : '程式驗證'
    default: return '—'
  }
}

/** When the event entered its current state (the last matching hop of its audit path), else when it opened. */
export function enteredAt(ev: Event, run: DayRun | undefined): string {
  const hop = [...(run?.path ?? [])].reverse().find((p) => p.state === ev.state)
  return hop?.ts ?? ev.opened_at
}

export const BOARD: EventState[] = ['detected', 'classify', 'plan', 'simulate', 'gate', 'await_approval', 'execute', 'verify', 'replan', 'closed', 'safe_stop']

// The page's plain view (CJ, 2026-10-22: 「狀態幾那麼多層真的搞不清楚在做什麼」): the 11 states fold into 4 stages a
// supervisor can read at a glance; the 11-state board stays behind 「看細節」.
export type Stage = 'ai' | 'you' | 'run' | 'end'
export const STAGES: { key: Stage; label: string; meaning: string; states: EventState[] }[] = [
  { key: 'ai', label: 'AI 處理中', meaning: 'AI 在查資料、想原因、排計劃', states: ['detected', 'classify', 'plan', 'simulate', 'gate'] },
  { key: 'you', label: '等你決定', meaning: '計劃好了，等你核准或討論', states: ['await_approval'] },
  { key: 'run', label: '執行與確認', meaning: '照核准的做，程式確認有沒有效；沒效會換假設重來', states: ['execute', 'verify', 'replan'] },
  { key: 'end', label: '結束', meaning: '結案，或 AI 停手交給人', states: ['closed', 'safe_stop'] },
]
export function stageOf(s: EventState): Stage {
  return STAGES.find((g) => g.states.includes(s))?.key ?? 'ai'
}

/** One plain sentence: where the event is and what it waits on, e.g. 「H1 被讀值否決，正在換假設 H2」. */
export function statusText(ev: Event, tasks: WorkTask[], mins: number): string {
  const open = tasks.filter((t) => t.event_id === ev.id && t.status !== '完成').length
  const v = (ev.prior_rounds?.length ?? 0) + 1
  switch (ev.state) {
    case 'detected': return '偵測到異常，AI 正在看資料'
    case 'classify': return 'AI 正在判斷是哪一類問題'
    case 'plan': return v > 1 ? `AI 正在排第 ${v} 版計劃` : 'AI 正在想原因、排計劃'
    case 'simulate': return `計劃 v${v} 排好了，程式正在試算影響`
    case 'gate': return `程式正在檢查計劃 v${v} 安不安全、要不要你核准`
    case 'await_approval': return `計劃 v${v} 等你核准（已等 ${mins} 分鐘）`
    case 'execute': return open ? `照核准的做，等現場完成 ${open} 項任務` : '照核准的計劃執行中'
    case 'verify': return open ? `等現場完成 ${open} 項任務，再由程式確認` : '程式正在看讀值，確認有沒有效'
    case 'replan': {
      const rejected = ev.plan?.rejected_hypothesis_ids.at(-1) ?? ev.hypotheses.find((h) => h.status === 'rejected')?.id ?? ev.plan?.hypothesis.id
      const next = ev.hypotheses.find((h) => h.status !== 'rejected' && h.id !== rejected)?.id
      return `${rejected ? `${rejected} 被讀值否決` : '上一個假設沒效'}，正在換假設${next ? ` ${next}` : ''}`
    }
    case 'closed': return '已結案'
    case 'safe_stop': return 'AI 停手，交給人處理'
  }
}
