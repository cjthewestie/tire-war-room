// Fast-repair notices on the supervisor phone (Kyle 10-21 flow: 核准 → 派人維修 → 維修完成通知 → 回溫 → 綠燈 → 異常報告).
// Nothing here waits for the supervisor, one card per event (3 件同時異常: one after another), a title and a sentence
// (veteran 10-22: no label chip, no time row, no banner above):
// - repaired (Event.repaired_at, not closed): 「7 號硫化機維修完成，溫度回升中」, the reading live in big type (the tile's
//   temperature every 0.5 s; other channels from the machine's last reading), what the crew did on one line
// - closed with its report unread (Event.review pending): 「7 號硫化機已恢復正常」 「溫度回到 128°C（02:17 維修完成）。Agent
//   Nemo 異常報告已產生。」, 「看報告」 (/report?event=<id>) and a small 「知道了」 (POST /api/events/{id}/review = 主管已讀報告,
//   PIN like approvals)
import { useState } from 'react'
import type { Event } from '../contracts'
import { CH_LABEL, expectationOf, normalOf, readingText, recoverWord, targetText } from '../aiStatus'
import { machineLabel } from '../factory'
import { fmtHM } from '../format'
import { plain } from '../plain'
import { useMachineDetail } from '../useMachineDetail'
import { useWarRoom } from '../useWarRoom'
import { isRepairTask } from './fastEta'
import { taskWhat } from './cardText'

/** 「溫度回到 128°C（02:17 維修完成）」 from the server's 「已修復：7 號機 溫度回到 128°C（維修完成 02:17、恢復 02:17）」; an
 *  unverified close keeps its own outcome, without the 「已修復：」 head. */
function backText(ev: Event): string {
  const back = ev.outcome?.match(/(\S*回到\s*[\d.]+\s*°?C?)/)?.[1]
  if (/^已修復/.test(ev.outcome ?? '') && back) return `${back}${ev.repaired_at ? `（${fmtHM(ev.repaired_at)} 維修完成）` : ''}`
  return plain(ev.outcome).replace(/^已修復[：:]\s*/, '')
}

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
  const repairs = (state.world?.tasks.items ?? []).filter((t) => t.event_id === ev.id && isRepairTask(t))
  const read = async () => {
    if (!transport?.review) return
    setBusy(true); setErr(null)
    try { onRead(await transport.review(ev.id)) } catch (e) { setErr(String(e).replace(/^Error: /, '')) } finally { setBusy(false) }
  }
  const name = machineLabel(ev.machine_id)
  if (report) {
    const back = backText(ev)
    return (
      <div className="ph-card ph-review" data-testid="phone-report" data-event={ev.id}>
        <h2 className="ph-title">{name}已恢復正常</h2>
        <p className="ph-lead">{back ? `${back}。` : ''}Agent Nemo 異常報告已產生。</p>
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
  const normal = normalOf(ev, x)
  const to = x ? (normal != null ? readingText(x.channel, normal) : targetText(x)) : ''
  return (
    <div className="ph-card ph-review" data-testid="phone-repaired" data-event={ev.id}>
      <h2 className="ph-title" data-testid="phone-repaired-title"><span className="ph-chunk">{name}維修完成，</span><span className="ph-chunk">{recoverWord(x, now, normal)}</span></h2>
      {x && now != null && (
        <p className="ph-big" aria-label={`${CH_LABEL[x.channel]} ${readingText(x.channel, now)}，正常值 ${to}`}>{readingText(x.channel, now)} → {to}</p>
      )}
      <p className="ph-lead">回到正常才轉綠，異常報告會自動產生。</p>
      <p className="ph-did muted" data-testid="phone-repaired-tasks">{repairs.length
        ? repairs.map((t) => [t.worker_name ?? t.team_label, taskWhat(t.title), t.status === '完成' && t.done_at ? `${fmtHM(t.done_at)} 完成` : t.status].join('・')).join('；')
        : '現場維修已完成'}</p>
    </div>
  )
}
