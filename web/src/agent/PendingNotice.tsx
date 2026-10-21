// The Agent chat's 待核准 notice (issue #100, phase 2): a short message instead of the big briefing card, which repeated
// what the center 計劃板 already shows. Desktop Console only — the shared BriefingView (phone, other states) is untouched.
// Everything comes from the event and its pending card: the machine / task name, the anomaly in words, whether a
// replacement schedule was proposed (only then does it say so), and the card's own time (created_at) places it in the chat.
import type { ApprovalRequest, Event } from '../contracts'
import { eventName } from '../briefing'
import { machineLabel } from '../factory'
import { fmtHM } from '../format'
import { plain } from '../plain'
import { proposedVersion } from './approvalView'

/** 「溫度下降 128→122.3°C」: the card's title without the machine name in front of it. */
export function anomalyLine(ev: Event, req: ApprovalRequest): string {
  const title = plain(req.title)
  const bare = title.replace(machineLabel(ev.machine_id), '').replace(/^[\s：:，,]+/, '')
  return bare || title
}

export function PendingNotice({ ev, req, onPlan }: { ev: Event; req: ApprovalRequest; onPlan: () => void }) {
  const ver = proposedVersion(ev, req)
  const v = req.version ?? 1
  return (
    <div className="con-notice" data-testid="con-brief" data-kind="pending-notice" data-event={ev.id} data-version={v}>
      <p className="cn-head"><i className="br-dot" aria-hidden="true" /><b>{eventName(ev)}</b><span aria-hidden="true">｜</span><b className="cn-st">待核准</b></p>
      <p className="cn-line">{anomalyLine(ev, req)}{/[。.]$/.test(anomalyLine(ev, req)) ? '' : '。'}</p>
      <p className="cn-line">{ver != null ? '已完成影響分析，替代排程等待主管核准。' : '已完成處置分析，等待主管核准。'}</p>
      <button type="button" className="cn-link" onClick={onPlan} data-testid="see-plan">查看處置方案 →</button>
      <small className="cn-ts">Agent・{fmtHM(req.created_at)}</small>
    </div>
  )
}
