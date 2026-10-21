// Issue #49: the shift handover, built by code from what the system already recorded (events, approvals, the
// world's tasks / materials / goods, the audit chain check). No model writes it, so every line can be traced.
import type { ApprovalRequest, Event, WorldSnapshot } from '../contracts'
import { STATE_LABEL } from '../components/common'
import { machineLabel } from '../factory'
import { fmtHM, fmtMD } from '../format'

export interface HandoverInput {
  now: string                      // factory time
  hours: number
  events: Event[]
  approvals: ApprovalRequest[]
  world: WorldSnapshot | null
  chainOk: boolean | null
}

export interface Line { time: string | null; text: string; tone?: 'ok' | 'warn' | 'bad' }
export interface Section { id: string; title: string; summary: string; lines: Line[] }
export interface Handover { from: string; to: string; sections: Section[]; text: string }

const DECISION: Record<string, string> = { approved: '核准', modified: '修改後核准', rejected: '否決', expired: '逾時', superseded: '被新版取代' }
const VIA: Record<string, string> = { phone: '手機', touchscreen: '現場螢幕', dashboard: '戰情室', auto: '自動' }
const MAT: Record<string, string> = { reorder: '已達再訂購點', low: '偏低', out: '缺料' }

const t = (iso: string | null | undefined) => (iso ? Date.parse(iso) : NaN)

function eventTitle(e: Event): string {
  return e.alert_card?.title || e.command || e.anomaly.message || e.goal || e.id
}

export function buildHandover(inp: HandoverInput): Handover {
  const end = t(inp.now)
  const start = end - inp.hours * 3600_000
  const within = (iso: string | null | undefined) => { const x = t(iso); return x >= start && x <= end }
  const sections: Section[] = []

  // events: opened in the window, or still open from before
  const evs = inp.events.filter((e) => within(e.opened_at) || (e.state !== 'closed' && e.state !== 'safe_stop' && t(e.opened_at) < start))
    .sort((a, b) => t(a.opened_at) - t(b.opened_at))
  const open = evs.filter((e) => e.state !== 'closed' && e.state !== 'safe_stop')
  const stopped = evs.filter((e) => e.state === 'safe_stop')
  sections.push({
    id: 'events', title: '事件',
    summary: evs.length ? `${evs.length} 件：結案 ${evs.length - open.length - stopped.length}、安全中止 ${stopped.length}、處理中 ${open.length}` : '這段時間沒有事件',
    lines: evs.map((e) => ({
      time: fmtHM(e.opened_at),
      text: `${machineLabel(e.machine_id)}：${eventTitle(e)}（${STATE_LABEL[e.state]}${e.replans ? `，換過 ${e.replans} 次判斷` : ''}）${e.outcome ? `→ ${e.outcome}` : ''}`,
      tone: e.state === 'closed' ? 'ok' : e.state === 'safe_stop' ? 'bad' : 'warn',
    })),
  })

  // approvals: decided in the window, plus the ones still waiting
  const decided = inp.approvals.filter((a) => a.status !== 'pending' && a.status !== 'superseded' && within(a.decision?.decided_at ?? a.created_at))
    .sort((a, b) => t(a.decision?.decided_at ?? a.created_at) - t(b.decision?.decided_at ?? b.created_at))
  const waiting = inp.approvals.filter((a) => a.status === 'pending')
  sections.push({
    id: 'approvals', title: '核准',
    summary: decided.length || waiting.length ? `決定 ${decided.length} 件、待核准 ${waiting.length} 件` : '這段時間沒有核准',
    lines: [
      ...decided.map((a) => {
        const d = a.decision
        const opt = a.options.find((o) => o.id === d?.option_id)
        const who = [d?.decided_by, d?.via ? VIA[d.via] ?? d.via : null].filter(Boolean).join('・')
        const rec = opt && !opt.recommended ? '（沒選系統建議）' : ''
        return {
          time: fmtHM(d?.decided_at ?? a.created_at),
          text: `${DECISION[a.status] ?? a.status}「${a.title}」${opt ? `：${opt.label}${rec}` : ''}${who ? `｜${who}` : ''}${d?.reason ? `｜理由：${d.reason}` : ''}`,
          tone: a.status === 'rejected' || a.status === 'expired' ? 'warn' : 'ok',
        } as Line
      }),
      ...waiting.map((a) => ({ time: fmtHM(a.created_at), text: `待核准「${a.title}」${a.expires_at ? `，${fmtHM(a.expires_at)} 前要決定` : ''}`, tone: 'warn' as const })),
    ],
  })

  // worker tasks
  const tasks = inp.world?.tasks.items ?? []
  const doneIn = tasks.filter((x) => x.status === '完成' && within(x.done_at))
  const openTasks = tasks.filter((x) => x.status !== '完成')
  sections.push({
    id: 'tasks', title: '人員任務',
    summary: doneIn.length || openTasks.length ? `完成 ${doneIn.length} 件、還沒完成 ${openTasks.length} 件` : '這段時間沒有派工',
    lines: openTasks.map((x) => ({ time: fmtHM(x.created_at), text: `${x.kind}：${x.title}｜${x.worker_name ?? '還沒派人'}（${x.status}）`, tone: 'warn' as const })),
  })

  // materials and orders
  const mats = (inp.world?.materials ?? []).filter((m) => m.status !== 'ok')
  const late = (inp.world?.goods.orders ?? []).filter((o) => o.late)
  sections.push({
    id: 'plant', title: '原料與訂單',
    summary: [mats.length ? `原料 ${mats.length} 項要注意` : '原料都正常', late.length ? `${late.length} 張單會晚交` : '訂單都在交期內'].join('、'),
    lines: [
      ...mats.map((m) => ({ time: null, text: `${m.name} ${m.stock.toLocaleString()} ${m.unit}（${MAT[m.status] ?? m.status}${m.on_order ? `，在途 ${m.on_order.toLocaleString()}` : ''}）`, tone: m.status === 'out' ? 'bad' as const : 'warn' as const })),
      ...late.map((o) => ({ time: null, text: `${o.order_id} 預計晚於交期 ${o.due}（${o.done}/${o.qty} 條）`, tone: 'warn' as const })),
    ],
  })

  sections.push({
    id: 'audit', title: '稽核紀錄',
    summary: inp.chainOk == null ? '讀取中' : inp.chainOk ? '雜湊鏈完整，紀錄沒有被改過' : '雜湊鏈驗證失敗，請通知主管',
    lines: [],
  })

  const from = new Date(start).toISOString()
  const sameDay = fmtMD(from) === fmtMD(inp.now)
  const head = `【交班】${fmtMD(from)} ${fmtHM(from)}–${sameDay ? '' : `${fmtMD(inp.now)} `}${fmtHM(inp.now)}（最近 ${inp.hours} 小時）`
  const text = [head, ...sections.flatMap((s) => [`■ ${s.title}：${s.summary}`, ...s.lines.map((l) => `・${l.time ? `${l.time} ` : ''}${l.text}`)])].join('\n')
  return { from, to: inp.now, sections, text }
}
