// 維修完成，請覆核 (fast repair, Kyle 10-21): an event that closed after its repair (Event.review pending) waits for the
// supervisor. The card says which machine, what the crew did (its repair tasks: who, when — from the world's task
// board), the reading now against the hypothesis' expectation, and the 公版 AI status line; one big 「覆核完成」 sends
// POST /api/events/{id}/review (PIN like approvals). Read-only otherwise.
import { useState } from 'react'
import type { Event } from '../contracts'
import { AiStatusLine } from '../AiStatusLine'
import { CH_LABEL, REPAIR_KINDS, expectationOf, meetsExpectation, readingText, targetText } from '../aiStatus'
import { machineLabel } from '../factory'
import { fmtHM, fmtHMS } from '../format'
import { plain } from '../plain'
import { useMachineDetail } from '../useMachineDetail'
import { useWarRoom } from '../useWarRoom'

export function ReviewCard({ ev, disabled, onDone }: { ev: Event; disabled: boolean; onDone: (ev: Event) => void }) {
  const { transport, state } = useWarRoom()
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const x = expectationOf(ev)
  const ch = x?.channel ?? 'temp_c'
  // the tile carries the temperature (live every 0.5 s); other channels come from the machine's last reading
  const detail = useMachineDetail(ch !== 'temp_c' ? ev.machine_id : null)
  const tile = state.snapshot?.machines.find((m) => m.machine_id === ev.machine_id)
  const now = ch === 'temp_c' ? tile?.temp_c ?? null : detail?.state.last_reading?.[ch] ?? null
  const ok = x && now != null ? meetsExpectation(x, now) : null
  const repairs = (state.world?.tasks.items ?? []).filter((t) => t.event_id === ev.id && REPAIR_KINDS.has(t.kind))
  const confirm = async () => {
    if (!transport?.review) return
    setBusy(true); setErr(null)
    try { onDone(await transport.review(ev.id)) } catch (e) { setErr(String(e).replace(/^Error: /, '')) } finally { setBusy(false) }
  }
  return (
    <div className="ph-card ph-review" data-testid="phone-review" data-event={ev.id}>
      <div className="ph-top">
        <span className="ph-risk ph-ok">維修完成</span>
        <span className="muted small">{ev.repaired_at ? `${fmtHMS(ev.repaired_at)} 故障排除` : ''}</span>
      </div>
      <h2 className="ph-title">維修完成，請覆核：{machineLabel(ev.machine_id)}</h2>
      {ev.outcome && <p className="ph-problem">{plain(ev.outcome)}</p>}
      <AiStatusLine ev={ev} className="ph-ai" />
      <dl className="ph-info">
        <dt>做了什麼</dt>
        <dd data-testid="phone-review-tasks">{repairs.length ? (
          <ul className="ph-acts-list">{repairs.map((t) => (
            <li key={t.id}>{t.title}・{t.worker_name ?? t.team_label}{t.status === '完成' && t.done_at ? `・${fmtHM(t.done_at)} 完成` : `・${t.status}`}</li>
          ))}</ul>
        ) : '現場維修已完成'}</dd>
        <dt>現在</dt>
        <dd data-testid="phone-review-reading">{now == null ? '讀值同步中…' : <>
          {CH_LABEL[ch]} <b>{readingText(ch, now)}</b>
          {x && <span className={ok ? 'ph-ok-t' : 'ph-warn-t'}>（目標 {targetText(x)}{ok ? '，已達標' : '，尚未達標'}）</span>}
        </>}</dd>
      </dl>
      {err && <p className="err">{err}</p>}
      <div className="ph-acts ph-review-acts">
        <button className="ph-approve" disabled={disabled || busy || !transport?.review} onClick={() => void confirm()} data-testid="phone-review-go">
          {busy ? '送出中…' : '覆核完成'}
        </button>
      </div>
      <p className="ph-foot">確認機台恢復正常後按下；覆核寫入稽核日誌。</p>
    </div>
  )
}
