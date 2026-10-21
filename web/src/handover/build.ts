// Issue #49: the shift handover, built by code from what the system already recorded (events, approvals, the
// world's tasks / materials / goods, the audit chain check). No model writes it, so every line can be traced.
// The first sentence answers 「交班要交代什麼」 (as 人力排程 v2: the answer on top); counts that are 0 are left out.
import type { ApprovalRequest, Event, WorldSnapshot } from '../contracts'
import { MACHINE_META, machineLabel } from '../factory'
import { fmtHM, fmtMD } from '../format'
import { workerName } from '../agent/subagents'
import { VIA, eventTitle, headline, openPhase, outcomeText, staleCards, tidy, who } from '../report/build'

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
export interface Handover { from: string; to: string; lead: Line; sections: Section[]; foot: string | null; text: string }

const DECISION: Record<string, string> = { approved: '核准', modified: '修改後核准', rejected: '否決', expired: '逾時', superseded: '被新版取代' }
const MAT: Record<string, string> = { reorder: '已達再訂購點', low: '偏低', out: '缺料' }

const t = (iso: string | null | undefined) => (iso ? Date.parse(iso) : NaN)
/** 「10 號硫化機」, or the title of a supervisor command / plant event */
const subject = (e: Event) => (e.machine_id in MACHINE_META ? machineLabel(e.machine_id) : eventTitle(e))
/** confirmed by a reading: its result reads 已恢復正常 */
const recovered = (e: Event) => e.state === 'closed' && outcomeText(e.machine_id, e.state, e.outcome).startsWith('已恢復正常')
/** 「核准全部：排程 v3 生效（…）；…」 → 「第 3 版排程生效」: the option's first clause */
const optionText = (label: string) => tidy(label.replace(/^核准(全部)?[：:]/, '').split(/[（；]/)[0])
/** 「已恢復正常 2、處理中 1」: only the counts that are not 0 */
const counts = (pairs: [string, number][], unit = '') => pairs.filter(([, n]) => n > 0).map(([k, n]) => `${k} ${n}${unit}`).join('、')

export function buildHandover(inp: HandoverInput): Handover {
  const end = t(inp.now)
  const start = end - inp.hours * 3600_000
  const within = (iso: string | null | undefined) => { const x = t(iso); return x >= start && x <= end }
  const sections: Section[] = []
  const tasks = inp.world?.tasks.items ?? []

  // events: opened in the window, or still open from before
  const evs = inp.events.filter((e) => within(e.opened_at) || (e.state !== 'closed' && e.state !== 'safe_stop' && t(e.opened_at) < start))
    .sort((a, b) => t(a.opened_at) - t(b.opened_at))
  const open = evs.filter((e) => e.state !== 'closed' && e.state !== 'safe_stop')
  const stopped = evs.filter((e) => e.state === 'safe_stop')
  const back = evs.filter(recovered)
  const allBack = evs.length > 0 && back.length === evs.length
  sections.push({
    id: 'events', title: '異常',
    summary: !evs.length ? '這段時間沒有異常' : allBack ? `${evs.length} 件，都已恢復正常`
      : `${evs.length} 件：${counts([['已恢復正常', back.length], ['已結束', evs.length - open.length - stopped.length - back.length], ['安全停止', stopped.length], ['處理中', open.length]])}`,
    lines: evs.map((e) => ({
      time: fmtHM(e.opened_at),
      text: `${eventTitle(e)} → ${open.includes(e) ? openPhase(e, tasks) : outcomeText(e.machine_id, e.state, e.outcome)}${e.state === 'closed' && e.repaired_at ? `（${fmtHM(e.repaired_at)} 維修完成）` : ''}`,
      tone: e.state === 'closed' ? 'ok' : e.state === 'safe_stop' ? 'bad' : 'warn',
    })),
  })

  // approvals: decided in the window, plus the ones still waiting
  const decided = inp.approvals.filter((a) => a.status !== 'pending' && a.status !== 'superseded' && within(a.decision?.decided_at ?? a.created_at))
    .sort((a, b) => t(a.decision?.decided_at ?? a.created_at) - t(b.decision?.decided_at ?? b.created_at))
  const waiting = inp.approvals.filter((a) => a.status === 'pending')
  // an approved card whose schedule went stale before it took effect (3 at once): never 「第 N 版排程生效」
  const redone = staleCards(inp.approvals)
  const evOf = (a: ApprovalRequest) => inp.events.find((e) => e.id === a.event_id)
  // 「10 號硫化機 溫度下降」: the card's title without the readings (the event line has them)
  const cardTitle = (a: ApprovalRequest) => headline(evOf(a)?.machine_id, a.title).replace(/\s*[\d.]+\s*→\s*[\d.]+\s*°C$/, '')
  sections.push({
    id: 'approvals', title: '核准',
    summary: decided.length || waiting.length ? counts([['決定', decided.length], ['待核准', waiting.length]], ' 件') : '這段時間沒有核准',
    lines: [
      ...decided.map((a) => {
        const d = a.decision
        const opt = a.options.find((o) => o.id === d?.option_id)
        const by = [d?.decided_by ? who(d.decided_by) : null, d?.via ? VIA[d.via] ?? d.via : null].filter(Boolean).join('・')
        const rec = opt && !opt.recommended ? '（沒選系統建議）' : ''
        const pick = opt && (a.options.length > 1 ? `方案 ${opt.id}（${optionText(opt.label)}）` : optionText(opt.label))
        const what = redone.has(a.id) ? '，排程已先被更新，已重算' : pick ? `：${pick}${rec}` : ''
        return {
          time: fmtHM(d?.decided_at ?? a.created_at),
          text: `${DECISION[a.status] ?? a.status}「${cardTitle(a)}」${what}${by ? `｜${by}` : ''}${d?.reason ? `｜理由：${tidy(d.reason)}` : ''}`,
          tone: a.status === 'rejected' || a.status === 'expired' ? 'warn' : 'ok',
        } as Line
      }),
      ...waiting.map((a) => ({ time: fmtHM(a.created_at), text: `待核准「${cardTitle(a)}」${a.expires_at ? `，${fmtHM(a.expires_at)} 前要決定` : ''}`, tone: 'warn' as const })),
    ],
  })

  // worker tasks
  const doneIn = tasks.filter((x) => x.status === '完成' && within(x.done_at))
  const openTasks = tasks.filter((x) => x.status !== '完成')
  // 待派 is not being done yet: a repair waits for 主管核准 (work.py hold), or for a free person
  const queued = openTasks.filter((x) => x.status === '待派').length
  const active = openTasks.length - queued
  sections.push({
    id: 'tasks', title: '現場工作',
    summary: !doneIn.length && !openTasks.length ? '這段時間沒有派工' : !openTasks.length ? `${doneIn.length} 件都完成`
      : counts([['完成', doneIn.length], ['在做', active], ['待派', queued]], ' 件'),
    lines: openTasks.map((x) => ({ time: fmtHM(x.created_at), text: `${x.kind}：${x.title}｜${x.worker_name ? workerName(x.worker_name) : '還沒派人'}（${x.status}）`, tone: 'warn' as const })),
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

  // the answer on top: what the next shift has to pick up (cards waiting, events not settled, materials short, orders
  // running late), or that nothing is left
  const waitingEv = new Set(waiting.map((a) => a.event_id))
  const todo = [
    ...waiting.map((a) => { const e = evOf(a); return `${e ? subject(e) : cardTitle(a)}待核准${a.expires_at ? `，${fmtHM(a.expires_at)} 前要決定` : ''}` }),
    ...open.filter((e) => !waitingEv.has(e.id)).map((e) => `${subject(e)}${openPhase(e, tasks)}`),
    ...stopped.map((e) => `${subject(e)}已安全停止，要人確認`),
    ...mats.map((m) => `${m.name}${MAT[m.status] ?? m.status}`),
    ...(late.length ? [`${late.length} 張單會晚交`] : []),
  ]
  const busy = active ? `還有 ${active} 件現場工作在做${queued ? `、${queued} 件待派` : ''}` : queued ? `${queued} 件現場工作待派` : ''
  const lead: Line = todo.length
    ? { time: null, text: `交班要交代 ${todo.length} 件：${todo.join('；')}。${busy ? `${busy}。` : ''}`, tone: 'warn' }
    : {
      time: null, tone: 'ok',
      text: `${!evs.length ? '這段時間沒有異常' : allBack ? `這段時間 ${evs.length} 件異常都已恢復正常` : `這段時間 ${evs.length} 件異常都已結束`}，${busy || '沒有要交代的事'}。`,
    }

  const foot = inp.chainOk == null ? null : inp.chainOk ? '稽核紀錄完整，沒有被改過' : '稽核紀錄驗證失敗，請通知主管'
  const from = new Date(start).toISOString()
  const sameDay = fmtMD(from) === fmtMD(inp.now)
  const head = `【交班】${fmtMD(from)} ${fmtHM(from)}–${sameDay ? '' : `${fmtMD(inp.now)} `}${fmtHM(inp.now)}（最近 ${inp.hours} 小時）`
  const text = [head, lead.text, ...sections.flatMap((s) => [`■ ${s.title}：${s.summary}`, ...s.lines.map((l) => `・${l.time ? `${l.time} ` : ''}${l.text}`)]), ...(foot ? [foot] : [])].join('\n')
  return { from, to: inp.now, lead, sections, foot, text }
}
