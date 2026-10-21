// 公版 AI 處理狀態 (Kyle 10-21: 「這得要是一個公版」): one sentence for every anomaly, the same on the phone, the 3D
// factory and the field SOP drawer —
//   AI 偵測到：<異常>｜建議交由 <agent>（<NemoClaw 沙箱>）修正｜<階段>｜預計 <時間> 完成（約 N 分鐘）
// Read-only: the agent is the main agent's subagent for this event (GET /api/subagents), the runtime label comes from
// /api/health agent_runtime (WARROOM_AGENT_RUNTIME, only set when the app really runs inside OpenShell), the time is
// the repair estimate of #73 (verification window from approval, or the last field task's ETA if later).
import type { Channel, Event, Expectation, WorkTask } from './contracts'
import { machineLabel } from './factory'
import { fmtHM, fmtMD, ms } from './format'
import { plain } from './plain'
import type { useWarRoom } from './useWarRoom'

type WarRoom = ReturnType<typeof useWarRoom>['state']

/** Field tasks that repair the machine itself (warroom/sim/work.py plan_tasks, runtime REPAIR_KINDS); staffing and 宣達
 *  are not repairs. */
export const REPAIR_KINDS = new Set(['檢修', '停用檢修', '放慢投料', '暫停投料'])
/** A task still 待派 this long after it was made is left out of the ETA: nobody took it, so its ETA only follows the
 *  clock (p.11 round 1's 調派支援 kept 「預計完成 02:46」 moving). */
const STALE_UNASSIGNED_MS = 10 * 60_000

export const CH_LABEL: Record<Channel, string> = { temp_c: '溫度', pressure_kgf_cm2: '壓力', water_flow_lpm: '水流', steam_kgf_cm2: '蒸汽壓' }
const UNIT: Record<Channel, string> = { temp_c: '°C', pressure_kgf_cm2: ' kgf/cm²', water_flow_lpm: ' L/min', steam_kgf_cm2: ' kgf/cm²' }

/** The expectation's target, e.g. 「≥121°C」. */
export function targetText(x: Expectation): string {
  const u = UNIT[x.channel]
  return x.op === 'between' ? `${x.value}–${x.value_hi}${u}` : `${x.op === '>=' ? '≥' : '≤'}${x.value}${u}`
}

/** A reading with its unit, e.g. 「100.2°C」. */
export function readingText(ch: Channel, v: number): string {
  return `${Math.round(v * 10) / 10}${UNIT[ch]}`
}

/** Does this reading meet the expectation (the same test VERIFY makes)? */
export function meetsExpectation(x: Expectation, v: number): boolean {
  return x.op === '>=' ? v >= x.value : x.op === '<=' ? v <= x.value : v >= x.value && v <= (x.value_hi ?? Infinity)
}

/** The expectation the event will be verified against: the plan's hypothesis first, else any open one. */
export function expectationOf(ev: Event | null): Expectation | null {
  if (!ev) return null
  return ev.plan?.hypothesis.expectation
    ?? ev.hypotheses.find((h) => h.status !== 'rejected' && h.expectation)?.expectation
    ?? null
}

/** The event's tasks that belong to its newest card: once a newer card exists (round 2 「7 號機停用」, a discussed v2),
 *  the older card's tasks are history (round 1's 宣達 28.5 h and 調派支援). Tasks with no card (R1 steps such as the
 *  檢修 notice, 撤回 notices) stay. */
function currentTasks(ev: Event, items: WorkTask[]): WorkTask[] {
  const card = ev.approvals.at(-1) ?? null
  return items.filter((t) => t.event_id === ev.id && (!t.approval_id || t.approval_id === card))
}

/** The latest ETA of these open tasks, leaving out the ones nobody has taken for STALE_UNASSIGNED_MS. */
function latestEta(open: WorkTask[], now: string | null): string | null {
  const live = open.filter((t) => !(t.status === '待派' && now && ms(now) - ms(t.created_at) > STALE_UNASSIGNED_MS))
  return live.map((t) => t.eta).filter((e): e is string => !!e).sort().at(-1) ?? null
}

/** When the anomaly should be fixed: from the approval (or now) + the verification window, or the last task ETA. */
export function repairEstimate(ev: Event, state: WarRoom) {
  const now = state.clock?.factory_time ?? state.snapshot?.header.factory_time ?? null
  const x = expectationOf(ev)
  const tasks = currentTasks(ev, state.world?.tasks.items ?? [])
  const open = tasks.filter((t) => t.status !== '完成')
  const lastEta = latestEta(open, now)
  // the newest card's decision: while it waits (round 2 pending) the window runs from now (「現在核准的話」); a screen
  // opened after the decision has no resolved card in its store, so the card's first task (made on approval) stands in
  const card = ev.approvals.at(-1)
  const waiting = !card || state.pending.some((p) => p.id === card)
  const decided = waiting ? null
    : state.resolved.find((r) => r.id === card && r.status === 'approved')?.decision?.decided_at
      ?? tasks.filter((t) => t.approval_id === card).map((t) => t.created_at).sort()[0] ?? null
  const from = decided ?? now
  const byWindow = x && from ? ms(from) + x.within_min * 60_000 : null
  const doneBy = lastEta ? Math.max(ms(lastEta), byWindow ?? 0) : byWindow
  return { now, x, tasks, open, lastEta, decided, doneBy }
}

/** What the AI detected: the detector's own text (the event's anomaly), not the latest alert title, which a later round
 *  rewrites into its action (p.11 round 2: 「7 號機停用，重排中」); the same text on every screen for the whole event.
 *  A supervisor command or plant event has no detection: its alert title as before. */
function firstDetection(ev: Event): string {
  if (ev.source === 'supervisor' || ev.source === 'plant') return ev.alert_card?.title ?? ev.anomaly.message
  return ev.anomaly.message || ev.alert_card?.title || machineLabel(ev.machine_id)
}

/** 「專人維修中（1/2）」: the repair tasks done out of all of them. */
const repairPhase = (repair: WorkTask[], open: WorkTask[]) => `專人維修中（${repair.length - open.length}/${repair.length}）`

/** A closed event is 「已修復」 only when a reading verified it: a repair task still running, a machine taken out
 *  (p.11 round 2: 「未驗證：已停用該機…」) or another unverified close says what really happened. A close after a repair
 *  waits for the supervisor's 覆核 (Event.review). */
function closedPhase(ev: Event, repair: WorkTask[], repairOpen: WorkTask[]): string {
  if (repairOpen.length) return repairPhase(repair, repairOpen)
  if (ev.review?.status === 'pending') return '已修復，待主管覆核'
  if (ev.review?.status === 'done') return '已修復・主管已覆核'
  if (/停用/.test(ev.outcome ?? '')) return '已停用該機，排程已調整'
  if (/未驗證/.test(ev.outcome ?? '')) return '已處置，未以讀值驗證'
  return '已修復'
}

const BEFORE_APPROVAL = new Set(['detected', 'classify', 'plan', 'simulate', 'gate', 'replan', 'await_approval'])

export function aiStatusParts(ev: Event, state: WarRoom, runtime: string | null) {
  const what = plain(firstDetection(ev)).replace(/^異常[：:]\s*/, '')
  const sub = state.subagents?.items.find((s) => s.id === ev.id)
  // Kyle 10-21: the single agent is called NemoClaw on screen; the kind of work stays in brackets
  const agent = `NemoClaw（${sub?.type_label ?? (ev.source === 'plant' ? '工廠調度' : ev.source === 'supervisor' ? '任務' : '機台處理')}）`
  const who = `${BEFORE_APPROVAL.has(ev.state) ? '建議交由' : '交由'} ${agent}${runtime ? `（${runtime}）` : ''} 修正`
  const est = repairEstimate(ev, state)
  const done = est.tasks.length - est.open.length
  // any round's repair task counts: the machine is not fixed while maintenance is still on it
  const repair = (state.world?.tasks.items ?? []).filter((t) => t.event_id === ev.id && REPAIR_KINDS.has(t.kind))
  const repairOpen = repair.filter((t) => t.status !== '完成')
  const phase =
    ev.state === 'closed' ? closedPhase(ev, repair, repairOpen)
      : ev.state === 'safe_stop' ? '已安全停止'
        : ev.state === 'await_approval' ? '等主管核准'
          : ev.state === 'execute' || ev.state === 'verify'
            ? (repairOpen.length ? repairPhase(repair, repairOpen) : est.open.length ? `現場修正中（${done}/${est.tasks.length}）` : '確認是否恢復')
            : ev.state === 'detected' || ev.state === 'classify' ? 'AI 分析中' : 'AI 擬定修正方案'
  // once closed, and while the crew repairs the machine, the ETA is the repair tasks' own (a fast repair takes seconds,
  // not the verification window)
  const repairing = ev.state === 'closed' || ((ev.state === 'execute' || ev.state === 'verify') && repairOpen.length > 0)
  const repairEta = repairing ? latestEta(repairOpen, est.now) : null
  const doneBy = ev.state === 'safe_stop' ? null : repairEta ? ms(repairEta) : ev.state === 'closed' ? null : est.doneBy
  let eta = ''
  if (doneBy) {
    const s = est.now ? (doneBy - ms(est.now)) / 1000 : null
    const left = s == null ? '' : s <= 0 ? '即將完成' : s < 60 ? `約 ${Math.max(1, Math.round(s))} 秒` : `約 ${Math.round(s / 60)} 分鐘`
    eta = `預計 ${fmtMD(doneBy)} ${fmtHM(doneBy)} 完成${left ? `（${left}）` : ''}`
  }
  return { what, who, phase, eta }
}
