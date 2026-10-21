// Fast-repair notices on the supervisor phone (Kyle 10-21 flow: 核准 → 專人維修 → 維修完成通知 → 回溫 → 綠燈 → 異常報告).
// Nothing here waits for the supervisor, one card per event (3 件同時異常: one after another):
// - repaired (Event.repaired_at, not closed): 「維修完成：<機台>，溫度回升中 <now> → <target>」, the reading live (the tile's
//   temperature every 0.5 s; other channels from the machine's last reading), what the crew did
// - closed with its report unread (Event.review pending): 「已恢復正常・異常報告已產生」, 「看報告」 (/report?event=<id>) and a
//   small 「知道了」 (POST /api/events/{id}/review = 主管已讀報告, PIN like approvals)
import { useState } from 'react'
import type { Event } from '../contracts'
import { REPAIR_KINDS, expectationOf, recoverText } from '../aiStatus'
import { machineLabel } from '../factory'
import { fmtHM, fmtHMS } from '../format'
import { plain } from '../plain'
import { useMachineDetail } from '../useMachineDetail'
import { useWarRoom } from '../useWarRoom'

export function RepairNotice({ ev, disabled, onRead }: { ev: Event; disabled: boolean; onRead: (ev: Event) => void }) {
  const { transport, state } = useWarRoom()
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const report = ev.state === 'closed'
  const x = expectationOf(ev)
  const ch = x?.channel ?? 'temp_c'
  const detail = useMachineDetail(!report && ch !== 'temp_c' ? ev.machine_id : null)
  const tile = state.snapshot?.machines.find((m) => m.machine_id === ev.machine_id)
  const now = ch === 'temp_c' ? tile?.temp_c ?? null : detail?.state.last_reading?.[ch] ?? null
  const repairs = (state.world?.tasks.items ?? []).filter((t) => t.event_id === ev.id && REPAIR_KINDS.has(t.kind))
  const read = async () => {
    if (!transport?.review) return
    setBusy(true); setErr(null)
    try { onRead(await transport.review(ev.id)) } catch (e) { setErr(String(e).replace(/^Error: /, '')) } finally { setBusy(false) }
  }
  const name = machineLabel(ev.machine_id)
  if (report) {
    const back = ev.recovered_at ?? ev.closed_at
    return (
      <div className="ph-card ph-review" data-testid="phone-report" data-event={ev.id}>
        <div className="ph-top">
          <span className="ph-risk ph-ok">已恢復正常</span>
          <span className="muted small">{[ev.repaired_at ? `維修完成 ${fmtHMS(ev.repaired_at)}` : '', back ? `恢復 ${fmtHMS(back)}` : ''].filter(Boolean).join('・')}</span>
        </div>
        <h2 className="ph-title">已恢復正常・異常報告已產生：{name}</h2>
        {ev.outcome && <p className="ph-problem">{plain(ev.outcome)}</p>}
        {err && <p className="err">{err}</p>}
        <div className="ph-acts ph-report-acts">
          <a className="ph-approve" href={`/report?event=${encodeURIComponent(ev.id)}`} target="_blank" rel="noopener" data-testid="phone-report-link">看報告</a>
          <button className="ph-sec" disabled={disabled || busy || !transport?.review} onClick={() => void read()} data-testid="phone-report-read">
            {busy ? '送出中…' : '知道了'}
          </button>
        </div>
      </div>
    )
  }
  return (
    <div className="ph-card ph-review" data-testid="phone-repaired" data-event={ev.id}>
      <div className="ph-top">
        <span className="ph-risk ph-ok">維修完成</span>
        <span className="muted small">{fmtHMS(ev.repaired_at)} 故障排除</span>
      </div>
      <h2 className="ph-title" data-testid="phone-repaired-title">維修完成：{name}，{recoverText(x, now)}</h2>
      <dl className="ph-info">
        <dt>做了什麼</dt>
        <dd data-testid="phone-repaired-tasks">{repairs.length ? (
          <ul className="ph-acts-list">{repairs.map((t) => (
            <li key={t.id}>{t.title}・{t.worker_name ?? t.team_label}{t.status === '完成' && t.done_at ? `・${fmtHM(t.done_at)} 完成` : `・${t.status}`}</li>
          ))}</ul>
        ) : '現場維修已完成'}</dd>
      </dl>
      <p className="ph-foot">讀值恢復正常後機台轉回綠燈，異常報告自動產生。</p>
    </div>
  )
}
