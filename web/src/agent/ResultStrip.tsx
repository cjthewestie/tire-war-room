// 核准之後 (主控台 design, agreed 2026-10-21, item 3): the approval card leaves the list as soon as it is decided and the
// plan area only flashed 「已核准」. This strip keeps the decision in view and follows its event —
// 「02:16 主管核准 B 從 2 線調 1 人 → 執行中 → 驗證中（30 分鐘內回到 121°C 以上）」 (fast repair: → 維修中・約 9 秒後維修完成 →
// 維修完成・溫度回升中), then 「✓ 已恢復正常：<讀值>」 — until the next event or approval arrives; the decision is also a row
// in 今天處理過的事 (HomeOverview).
import type { Event } from '../contracts'
import { aiStatusParts, repairStage } from '../aiStatus'
import { STATE_LABEL } from '../components/common'
import { machineLabel } from '../factory'
import { fmtHM } from '../format'
import { useHealth } from '../health'
import { latestDecision, useHistory } from '../history'
import { backText } from '../phone/alert'
import { plain } from '../plain'
import { useWarRoom } from '../useWarRoom'
import { expectationText } from './transcript'

interface Stage { label: string; st: 'done' | 'on' | 'todo' }

export function ResultStrip({ ev, subject, onFull }: { ev: Event; subject?: string; onFull?: (eventId?: string) => void }) {
  const h = useHistory()
  const { state } = useWarRoom()
  const fastS = useHealth()?.repair_fast_s ?? null
  const d = latestDecision(h, ev.id)
  // a new card for the same event (換假設 → next round) is decided in the plan area itself
  if (!d || state.pending.some((p) => p.event_id === ev.id)) return null
  const closed = ev.state === 'closed', stop = ev.state === 'safe_stop'
  const rejected = d.tone === 'no'
  const tasks = (state.world?.tasks?.items ?? []).filter((t) => t.event_id === ev.id)
  const tdone = tasks.filter((t) => t.status === '完成').length
  // after the decision: execute → verify → closed; any other state means the check failed and the AI re-plans
  const verifying = ev.state === 'verify'
  const executing = ev.state === 'execute' || ev.state === 'await_approval'
  const failed = !rejected && !closed && !stop && !verifying && !executing
  // a failed check: the plan already carries the next hypothesis, so name the expectation that was not met
  const exp = expectationText((failed ? ev.hypotheses.filter((x) => x.status === 'rejected').pop() : null) ?? ev.plan?.hypothesis ?? null)
  const check = exp ?? '程式比對讀值'
  const work = tasks.length ? `（現場工作 ${tdone}/${tasks.length}）` : ''
  // fast repair (Kyle 10-21 flow): the event stays in verify through the repair and the ramp back; say what the phone and
  // the 3D say (aiStatusParts: 維修中・約 9 秒後維修完成, 維修完成・溫度回升中), not the 30-minute verification window
  const stage = fastS && fastS > 0 && !closed && !stop ? repairStage(ev, state.world?.tasks.items ?? []) : null
  const fast = stage === 'repairing' || stage === 'recovering' ? aiStatusParts(ev, state, null, fastS) : null
  const fastLabel = fast ? [fast.phase.replace(/（\d+\/\d+）$/, ''), stage === 'repairing' ? fast.eta : ''].filter(Boolean).join('・') : ''
  const stages: Stage[] = rejected || closed || stop ? [] : fast
    ? [{ label: `已執行${work}`, st: 'done' }, { label: fastLabel, st: 'on' }]
    : executing
    ? [{ label: `執行中${work}`, st: 'on' }, { label: `驗證（${check}）`, st: 'todo' }]
    : [{ label: `已執行${work}`, st: 'done' }, ...(verifying ? [{ label: `驗證中（${check}）`, st: 'on' as const }] : [])]
  // veteran 10-22: 「✓ 已恢復正常：7 號硫化機溫度回到 128°C（02:18 維修完成）」 once a reading confirmed it; no 已結案 on 主控台
  const back = closed && ev.repaired_at && /^已修復/.test(ev.outcome ?? '') ? backText(ev) : null
  const end = closed ? (back ? `✓ 已恢復正常：${/^\S{1,4}回到/.test(back) ? machineLabel(ev.machine_id) : ''}${back}` : `✓ 已處理完：${plain(ev.outcome ?? '完成')}`)
    : stop ? `■ 安全停止：${plain(ev.outcome ?? '安全核心已停機')}`
    : failed ? `✗ 驗證未達標（${check}）→ AI ${STATE_LABEL[ev.state]}中` : rejected ? `→ AI ${STATE_LABEL[ev.state]}中` : null
  const tone = closed ? 'ok' : stop || failed ? 'bad' : 'run'
  const full = (e: React.MouseEvent) => { if (onFull) { e.preventDefault(); onFull(ev.id) } }
  return (
    <div className={`rs rs-${tone}`} role="status" aria-live="polite" data-testid="result-strip" data-state={ev.state}>
      <span className="rs-dec"><b>{fmtHM(d.at)}</b> {subject && <>{subject}・</>}{d.text}</span>
      {stages.map((s) => (
        <span key={s.label} className={`rs-s ${s.st}`}><i aria-hidden="true">→</i>{s.st === 'on' && <i className="spin" />}{s.label}</span>
      ))}
      {end && <span className="rs-end">{end}</span>}
      {onFull && <a className="rs-link" href={`#console?event=${encodeURIComponent(ev.id)}`} onClick={full}>看完整過程 →</a>}
    </div>
  )
}
