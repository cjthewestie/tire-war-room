// 核准之後 (主控台 design, agreed 2026-10-21, item 3): the approval card leaves the list as soon as it is decided and the
// plan area only flashed 「已核准」. This strip keeps the decision in view and follows its event (10-21 flow) —
// 「02:16 主管核准方案 B（手機）→ 維修中（0/1）→ 02:16 維修完成 → 回溫中」, then 「✓ 已恢復正常：7 號硫化機溫度回到 128°C
// （02:16 維修完成）」 — until the next event or approval arrives; the decision is also a row in 今天處理過的事 (HomeOverview).
import type { Event } from '../contracts'
import { REPAIR_KINDS } from '../aiStatus'
import { fmtHM } from '../format'
import { latestDecision, useHistory } from '../history'
import { useWarRoom } from '../useWarRoom'
import { ended, outcomeText } from './homeItems'
import { expectationText } from './transcript'

interface Stage { label: string; st: 'done' | 'on' | 'todo' }

/** 「主管核准方案 B（手機）」: the option letter only (its label is on the card and in 今天處理過的事); a single-option card
 *  is just 「主管核准（手機）」. */
function decisionWords(text: string): string {
  if (/「/.test(text)) return text                      // 駁回「…」 and 逾時 name the card: keep them
  const via = /（[^（）]+）$/.exec(text)?.[0] ?? ''
  const m = /^(.*?核准) ([A-Z]) /.exec(text)
  if (m) return `${m[1]}方案 ${m[2]}${via}`
  const v = /^(.*?核准)/.exec(text)
  return v ? `${v[1]}${via}` : text
}

/** 「7 號硫化機溫度回到 128°C（02:16 維修完成）」 from the backend's 「已修復：7 號機 溫度回到 128°C（維修完成 02:16、恢復 02:17）」. */
function endText(ev: Event): string {
  return outcomeText(ev).replace(/^(\d+) 號機\s*/, '$1 號硫化機').replace(/（維修完成 (\d{1,2}:\d{2})(?:、恢復 \d{1,2}:\d{2})?）/, '（$1 維修完成）')
}

export function ResultStrip({ ev, subject, onFull }: { ev: Event; subject?: string; onFull?: (eventId?: string) => void }) {
  const h = useHistory()
  const { state } = useWarRoom()
  const d = latestDecision(h, ev.id)
  // a new card for the same event (重新判斷 → next round, or recomputed) is decided in the plan area itself
  if (!d || state.pending.some((p) => p.event_id === ev.id)) return null
  const closed = ev.state === 'closed', stop = ev.state === 'safe_stop'
  const rejected = d.tone === 'no'
  const tasks = (state.world?.tasks?.items ?? []).filter((t) => t.event_id === ev.id)
  const tdone = tasks.filter((t) => t.status === '完成').length
  const repair = tasks.filter((t) => REPAIR_KINDS.has(t.kind))
  const repairDone = repair.filter((t) => t.status === '完成').length
  // after the decision: execute (repair) → verify → closed; any other state means Agent Nemo is planning again
  const verifying = ev.state === 'verify'
  const executing = ev.state === 'execute' || ev.state === 'await_approval'
  const replanning = !rejected && !closed && !stop && !verifying && !executing
  // a reading that missed the expectation rejected the hypothesis: name the expectation that was not met
  const missed = replanning ? ev.hypotheses.filter((x) => x.status === 'rejected').pop() ?? null : null
  const check = expectationText(missed ?? ev.plan?.hypothesis ?? null) ?? '讀值回到正常'
  // the repair runs while the event executes or verifies: until it is done, that is the stage
  const repairing = repairDone < repair.length && !ev.repaired_at
  // closed, but people are still repairing it or still on a supervisor task's work: not finished yet (ended, homeItems.ts)
  const fin = closed ? ended(ev, tasks) : null
  const working = fin?.tone === 'run'
  const stages: Stage[] = working ? [{ label: fin!.pill, st: 'on' }]
    : rejected || closed || stop || replanning ? []
    : ev.repaired_at ? [{ label: `${fmtHM(ev.repaired_at)} 維修完成`, st: 'done' }, { label: ev.recovered_at ? '已恢復正常' : '回溫中', st: ev.recovered_at ? 'done' : 'on' }]
    : repairing || executing ? [{ label: repairing ? `維修中（${repairDone}/${repair.length}）` : tasks.length ? `現場工作 ${tdone}/${tasks.length}` : '執行中', st: 'on' }, { label: `確認恢復（${check}）`, st: 'todo' }]
    : [{ label: tasks.length ? `現場工作 ${tdone}/${tasks.length}` : '已執行', st: 'done' }, { label: `讀值確認中（${check}）`, st: 'on' }]
  const end = working ? null : closed ? `✓ ${fin!.pill}：${endText(ev)}` : stop ? `■ 安全停止：${outcomeText(ev) || '安全核心已停機'}`
    : missed ? `✗ 讀值沒有達到（${check}）→ Agent Nemo 重新判斷中` : replanning || rejected ? 'Agent Nemo 重新規劃中' : null
  const tone = closed && !working ? 'ok' : stop || missed ? 'bad' : 'run'
  const full = (e: React.MouseEvent) => { if (onFull) { e.preventDefault(); onFull(ev.id) } }
  return (
    <div className={`rs rs-${tone}`} role="status" aria-live="polite" data-testid="result-strip" data-state={ev.state}>
      <span className="rs-dec"><b>{fmtHM(d.at)}</b> {subject && <>{subject}・</>}{decisionWords(d.text)}</span>
      {stages.map((s) => (
        <span key={s.label} className={`rs-s ${s.st}`}><i aria-hidden="true">→</i>{s.st === 'on' && <i className="spin" />}{s.label}</span>
      ))}
      {end && <span className="rs-end"><i aria-hidden="true">→</i> {end}</span>}
      {onFull && <a className="rs-link" href={`#console?event=${encodeURIComponent(ev.id)}`} onClick={full}>看完整過程 →</a>}
    </div>
  )
}
