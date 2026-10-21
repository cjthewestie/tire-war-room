// 公版 AI 處理狀態 (Kyle 10-21: 「這得要是一個公版」): one sentence for every anomaly, the same on the phone, the 3D
// factory and the field SOP drawer —
//   AI 偵測到：<異常>｜建議交由 <agent>（<NemoClaw 沙箱>）修正｜<階段>｜預計 <時間> 完成（約 N 分鐘）
// Read-only: the agent is the main agent's subagent for this event (GET /api/subagents), the runtime label comes from
// /api/health agent_runtime (WARROOM_AGENT_RUNTIME, only set when the app really runs inside OpenShell), the time is
// the repair estimate of #73 (verification window from approval, or the last field task's ETA if later).
import type { Event, Expectation, WorkTask } from './contracts'
import { machineLabel } from './factory'
import { fmtHM, fmtMD, ms } from './format'
import { plain } from './plain'
import type { useWarRoom } from './useWarRoom'

type WarRoom = ReturnType<typeof useWarRoom>['state']

/** The expectation the event will be verified against: the plan's hypothesis first, else any open one. */
export function expectationOf(ev: Event | null): Expectation | null {
  if (!ev) return null
  return ev.plan?.hypothesis.expectation
    ?? ev.hypotheses.find((h) => h.status !== 'rejected' && h.expectation)?.expectation
    ?? null
}

/** When the anomaly should be fixed: from the approval (or now) + the verification window, or the last task ETA. */
export function repairEstimate(ev: Event, state: WarRoom) {
  const now = state.clock?.factory_time ?? state.snapshot?.header.factory_time ?? null
  const x = expectationOf(ev)
  const tasks: WorkTask[] = (state.world?.tasks.items ?? []).filter((t) => t.event_id === ev.id)
  const open = tasks.filter((t) => t.status !== '完成')
  const lastEta = open.map((t) => t.eta).filter((e): e is string => !!e).sort().at(-1) ?? null
  const decided = state.resolved.filter((r) => r.event_id === ev.id && r.status === 'approved' && r.decision?.decided_at)
    .map((r) => r.decision!.decided_at).sort().at(-1) ?? null
  const from = decided ?? now
  const byWindow = x && from ? ms(from) + x.within_min * 60_000 : null
  const doneBy = lastEta ? Math.max(ms(lastEta), byWindow ?? 0) : byWindow
  return { now, x, tasks, open, lastEta, decided, doneBy }
}

const BEFORE_APPROVAL = new Set(['detected', 'classify', 'plan', 'simulate', 'gate', 'replan', 'await_approval'])

export function aiStatusParts(ev: Event, state: WarRoom, runtime: string | null) {
  const what = plain(ev.alert_card?.title ?? ev.anomaly.message ?? machineLabel(ev.machine_id)).replace(/^異常[：:]\s*/, '')
  const sub = state.subagents?.items.find((s) => s.id === ev.id)
  const agent = `${sub?.type_label ?? (ev.source === 'plant' ? '工廠調度' : ev.source === 'supervisor' ? '任務' : '機台處理')} agent`
  const who = `${BEFORE_APPROVAL.has(ev.state) ? '建議交由' : '交由'} ${agent}${runtime ? `（${runtime}）` : ''} 修正`
  const est = repairEstimate(ev, state)
  const done = est.tasks.length - est.open.length
  const phase =
    ev.state === 'closed' ? '已修復'
      : ev.state === 'safe_stop' ? '已安全停止'
        : ev.state === 'await_approval' ? '等主管核准'
          : ev.state === 'execute' || ev.state === 'verify'
            ? (est.open.length ? `現場修正中（${done}/${est.tasks.length}）` : '確認是否恢復')
            : ev.state === 'detected' || ev.state === 'classify' ? 'AI 分析中' : 'AI 擬定修正方案'
  let eta = ''
  if (ev.state !== 'closed' && ev.state !== 'safe_stop' && est.doneBy) {
    const left = est.now ? Math.max(1, Math.round((est.doneBy - ms(est.now)) / 60_000)) : null
    eta = `預計 ${fmtMD(est.doneBy)} ${fmtHM(est.doneBy)} 完成${left ? `（約 ${left} 分鐘）` : ''}`
  }
  return { what, who, phase, eta }
}
