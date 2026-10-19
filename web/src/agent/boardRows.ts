// The departure-board row model: one "train" per event, most urgent first (see DepartureBoard.tsx, DESIGN.md).
import type { ApprovalRequest, Event, EventState } from '../contracts'
import { machineLabel } from '../factory'
import { fmtHMS } from '../format'
import { plain } from '../plain'
import { STATE_LABEL } from '../components/common'

export type Tone = 'wait' | 'bad' | 'ok' | 'run' | 'done'
export interface BoardRowData {
  id: string               // event id
  time: string             // HH:MM
  machine: string
  status: string
  tone: Tone
  next: string
  owner: string
  sev: 1 | 2 | 3           // size/weight ramp: 3 critical, 2 needs attention, 1 routine
  sig: string              // what makes the row flip: status | plan version | approval
}

const OPEN_RANK: Record<EventState, number> = {
  safe_stop: 0, await_approval: 1, replan: 2, detected: 3, classify: 3, plan: 3, simulate: 3, gate: 3, execute: 4, verify: 4, closed: 9,
}
const OWNER: Partial<Record<EventState, string>> = {
  detected: 'AI 指揮官', classify: 'AI 指揮官', plan: 'AI 指揮官', simulate: '排程求解', gate: '程式把關', await_approval: '主管',
  execute: '系統執行', verify: '程式驗證', replan: 'AI 指揮官', safe_stop: '安全核心',
}
const NEXT: Partial<Record<EventState, string>> = {
  detected: '判斷原因、擬計畫', classify: '判斷原因、擬計畫', plan: '擬處理計畫', simulate: '模擬排程', gate: '檢查會改變現場的步驟',
  await_approval: '等主管核准', execute: '執行核准內容', verify: '比對預期與讀值', replan: '換假設重新規劃', safe_stop: '安全停止，待現場處理',
}

function shortMachine(id: string): string {
  return machineLabel(id).replace('硫化機', '').replace(/\s+/g, ' ')
}

/** One row per event, most urgent first: pending approvals (R3 before R2, soonest deadline first), then a safe
 *  stop, then open events newest first, then up to `closedN` closed ones. */
export function boardRows(events: Event[], pending: ApprovalRequest[], closedN = 2): BoardRowData[] {
  const pendBy = new Map<string, ApprovalRequest>()
  for (const p of pending) { const o = pendBy.get(p.event_id); if (!o || (p.version ?? 1) > (o.version ?? 1)) pendBy.set(p.event_id, p) }
  const rows = events.map((ev) => {
    const req = pendBy.get(ev.id)
    const closed = ev.state === 'closed'
    let row: BoardRowData
    if (req) {
      row = {
        id: ev.id, time: fmtHMS(req.created_at).slice(0, 5), machine: shortMachine(ev.machine_id), status: `待核准${(req.version ?? 1) > 1 ? ` v${req.version}` : ''}`,
        tone: 'wait', next: plain(req.title), owner: '主管', sev: req.risk === 'R3' ? 3 : 2, sig: `${req.id}|${req.version ?? 1}|${req.status}`,
      }
    } else {
      const running = ev.steps.find((s) => s.state === 'running')
      const next = closed ? plain(ev.outcome ?? '已結案') : ev.state === 'execute' && running ? plain(running.step.purpose) : NEXT[ev.state] ?? STATE_LABEL[ev.state]
      row = {
        id: ev.id, time: fmtHMS(ev.opened_at).slice(0, 5), machine: shortMachine(ev.machine_id), status: STATE_LABEL[ev.state],
        tone: ev.state === 'safe_stop' ? 'bad' : closed ? 'done' : ev.state === 'verify' || ev.state === 'execute' ? 'ok' : 'run',
        next, owner: closed ? '—' : OWNER[ev.state] ?? '—',
        sev: ev.state === 'safe_stop' || (!closed && ev.anomaly.severity === 'critical') ? 3 : closed ? 1 : 2,
        sig: `${ev.state}|${ev.replans}|${ev.approvals.length}`,
      }
    }
    return { row, ev, req }
  })
  const rank = (x: (typeof rows)[number]) => (x.req ? (x.req.risk === 'R3' ? -2 : -1) : OPEN_RANK[x.ev.state] ?? 5)
  const open = rows.filter((x) => x.ev.state !== 'closed').sort((a, b) => rank(a) - rank(b)
    || (a.req && b.req ? (a.req.expires_at ?? '9').localeCompare(b.req.expires_at ?? '9') : 0)
    || (a.ev.opened_at < b.ev.opened_at ? 1 : -1))
  const closed = rows.filter((x) => x.ev.state === 'closed').sort((a, b) => ((a.ev.closed_at ?? a.ev.opened_at) < (b.ev.closed_at ?? b.ev.opened_at) ? 1 : -1)).slice(0, closedN)
  return [...open, ...closed].map((x) => x.row)
}
