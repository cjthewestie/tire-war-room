// 主控台 approval card, answer first (veteran 10-22 text for HY): one line that says what to approve and by when, one line
// with Agent Nemo's recommendation (派人維修 first: it is the demo's protagonist), then the blocks in the order a
// supervisor decides — 選人, 交期, 誰去做. Everything is built from the card, the event and the field tasks; nothing is
// hard-coded, and a missing field leaves its part out. Only the plan layout (主控台) uses it; 產線監控 keeps its card.
import type { ApprovalRequest, Event, WorkTask } from '../contracts'
import { CH_LABEL, REPAIR_KINDS, readingText } from '../aiStatus'
import { TEAM_LABELS, machineLabel } from '../factory'
import { factoryMidnight, fmtHM, fmtMD, ms } from '../format'
import { plain } from '../plain'
import { TOOL_LABEL } from '../stepText'
import { cureChange, deliveryFacts, type OptionView, type PlanNode, type ScheduleDiff } from './approvalView'

const STAFF_KINDS = new Set(['overtime', 'staff_move', 'transfer'])
const isStaffing = (o: OptionView) => STAFF_KINDS.has(String(o.kind ?? ''))
const hours = (h: number) => `${Math.round(h * 10) / 10} 小時`
const pressNo = (id: string) => (id.startsWith('CURE-') ? `${Number(id.slice(5))} 號硫化機` : machineLabel(id))

/** 「03:54 前」, with the day when the deadline is not today (factory time). */
export function deadline(iso: string | null, now: number): string | null {
  if (!iso) return null
  const t = ms(iso)
  const today = isFinite(now) && t >= factoryMidnight(now) && t < factoryMidnight(now) + 86_400_000
  return `${today ? '' : `${fmtMD(iso)} `}${fmtHM(iso)} 前`
}

/** 「7 號硫化機溫度降到 122°C」: the detector's reading against its baseline; null for a supervisor or plant event. */
function readingWhat(ev: Event | null): string | null {
  const a = ev?.anomaly
  if (!ev || ev.source === 'supervisor' || ev.source === 'plant' || !a?.channel || !(a.baseline > 0) || a.value == null) return null
  const v = a.channel === 'temp_c' ? `${Math.round(a.value)}°C` : readingText(a.channel, a.value)
  return `${machineLabel(ev.machine_id)}${CH_LABEL[a.channel]}${a.value < a.baseline ? '降到' : '升到'} ${v}`
}

/** 「請核准：7 號硫化機溫度降到 122°C（03:54 前）」 */
export function askLine(req: ApprovalRequest, ev: Event | null, now: number): string {
  const by = deadline(req.expires_at, now)
  return `請核准：${readingWhat(ev) ?? plain(req.title).replace(/^異常[：:]\s*/, '')}${by ? `（${by}）` : ''}`
}

/** The repair the approval releases (the task waits 待派 until then): 「檢查蒸汽供應與溫度計」 from its title. */
function repairWhat(t: WorkTask): string {
  const m = /請(.+?)。?$/.exec(t.title)
  if (m) return m[1]
  return t.kind === '檢修' ? '維修' : t.kind === '停用檢修' ? '停機檢修' : t.kind
}
export function repairTask(ev: Event | null, tasks: WorkTask[]): WorkTask | null {
  return ev ? tasks.find((t) => t.event_id === ev.id && REPAIR_KINDS.has(t.kind) && t.status !== '完成') ?? null : null
}

/** The machine the plan takes out of service (round 2: 「7 號機停用」), from its notify step. */
function machineOut(ev: Event | null): string | null {
  const out = ev?.plan?.steps.find((s) => s.tool === 'notify_teams' && s.args.kind === 'machine_out')
  if (!out || !ev) return null
  return typeof out.args.machine_id === 'string' ? out.args.machine_id : ev.machine_id
}

/** 交期 in a few words: the solver's verdict first, else the card's own line. */
export function deliveryWords(req: ApprovalRequest, ev: Event | null): string {
  const f = deliveryFacts(ev)
  if (f.late.length) return `${f.late.length} 張會晚交`
  if (f.met === true) return '全部準時'
  const d = plain(req.delivery_impact)
  return /排程未變更/.test(d) ? '排程不變' : d || '—'
}

/** 「Agent Nemo 建議：派人檢查蒸汽供應與溫度計，這一模硫化延長到 28.5 小時，人力用方案 B（從 2 線調 1 人）；交期全部準時」 */
export function recommendLine(req: ApprovalRequest, ev: Event | null, tasks: WorkTask[], opts: OptionView[]): string {
  const parts: string[] = []
  const rep = repairTask(ev, tasks)
  if (rep) parts.push(`派人${repairWhat(rep)}`)
  const out = machineOut(ev)
  const cure = cureChange(ev)
  if (out) parts.push(`${machineLabel(out)}停用交維修，其他模次改由別台`)
  else if (cure) parts.push(`這一模硫化延長到 ${hours(cure.to)}`)
  const staff = opts.filter(isStaffing)
  const rec = staff.find((o) => o.opt.recommended)
  if (staff.length > 1 && rec) parts.push(`人力用方案 ${rec.opt.id}（${rec.opt.label}）`)
  // a single option that is the action itself (「放慢生料投入」), not the 「核准全部：…」 dump of the steps
  if (opts.length === 1 && !isStaffing(opts[0]) && !/^核准全部/.test(opts[0].opt.label)) parts.push(plain(opts[0].opt.label))
  const d = deliveryWords(req, ev)
  const del = d === '全部準時' ? '交期全部準時' : /會晚交$/.test(d) ? d : d === '排程不變' ? d : `交期${d}`
  return `${parts.join('，') || plain(req.reason)}；${del}`
}

/** One line per staffing option: what it changes (SOP night-shift-staffing: 加班 低風險 / 跨組調人 高風險). */
export function optionLine(o: OptionView): string {
  const ppl = o.people ?? 1
  const shift = o.shift ?? ''
  if (o.kind === 'overtime') return `${o.who?.replace(/（原班人員加班）$/, '') ?? '原組'}原班加班 ${ppl} 人補 ${shift}，其他產線不受影響・風險較低`
  if (o.kind === 'transfer' || o.kind === 'staff_move') {
    const from = o.who?.split(' → ')[0] ?? '其他組'
    return `${from} ${shift}少 ${ppl} 人，可能影響產能・風險較高（跨組調人）`
  }
  return plain(o.message ?? o.opt.label)
}

/** Why the system suggests this option: the supervisor's own preference, the previous card's choice, else the solver's
 *  rule (warroom/schedule/solver.py: a transfer when allowed, no overtime cost). */
export function whyLine(req: ApprovalRequest, ev: Event | null, rec: OptionView, staff: OptionView[]): string {
  if (ev?.constraints?.some((c) => c.kind === 'prefer_option')) return '照你先前說的偏好'
  if ((req.changes ?? []).some((c) => /沿用/.test(c))) return '沿用你上次選的方案'
  const other = staff.find((o) => o !== rec)
  if (rec.kind === 'transfer' || rec.kind === 'staff_move') return `不用付加班費（排程求解器優先調人）${other ? `；想降低風險可改選 ${other.opt.id}` : ''}`
  return '不動其他組，風險較低'
}

/** 「A、B 只差夜班由誰補人，其他處置和交期都一樣。」 — only when every option is a staffing choice (the card has one
 *  delivery line for all of them, so the deliveries are the same). */
export function guideLine(opts: OptionView[]): string | null {
  if (opts.length < 2 || !opts.every(isStaffing)) return null
  const shift = opts[0].shift?.split(' ').pop() ?? ''
  return `${opts.map((o) => o.opt.id).join('、')} 只差${shift}由誰補人，其他處置和交期都一樣。`
}

export interface WhoRow { who: string; what: string }

/** 核准後誰去做, in plain words and from the data (not the backend's action strings when the data says it better):
 *  the schedule version with its moves, the cure time on the press panel, the repair, the chosen staffing option and
 *  the notices that run by themselves after approval. Any other action line is kept, split at its 「誰：」. */
export function whoRows(req: ApprovalRequest, ev: Event | null, tasks: WorkTask[], chosen: OptionView | null, diff: ScheduleDiff | null, after: PlanNode[]): WhoRow[] {
  const rows: WhoRow[] = []
  const cure = cureChange(ev)
  for (const raw of req.actions ?? []) {
    const a = plain(raw)
    if (/^人力：依所選方案/.test(a)) continue              // the chosen option below says it
    const v = /排程\s*v(\d+)\s*生效/.exec(raw)
    if (v) {
      const bits = [
        ...(cure ? [`${pressNo(cure.press)}這一模硫化 ${hours(cure.to)}`] : []),
        ...(diff?.moved ?? []).map((m) => `${m.order} 改到 ${pressNo(m.to)}${m.n > 1 ? ` ${m.n} 模` : ''}`),
      ]
      rows.push({ who: '系統', what: `改用第 ${v[1]} 版排程${bits.length ? `（${bits.join('；')}）` : ''}` })
      continue
    }
    if (/硫化總時數/.test(a) && cure) { rows.push({ who: '硫化機組', what: `在 ${pressNo(cure.press)}面板輸入硫化 ${hours(cure.to)}` }); continue }
    // 「放慢生料投入 → 胎側膠組［B 線開煉機］：B 線開煉機膠溫偏高，請…」
    const n = /^.+? → ([^［：]+)(?:［[^］]*］)?：(.+)$/.exec(a)
    if (n) { rows.push({ who: n[1], what: n[2] }); continue }
    const m = /^([^：:（(]{1,8})[：:]\s*(.+)$/.exec(a)
    rows.push(m ? { who: m[1], what: m[2] } : { who: '系統', what: a.replace(/（公式 [\d.]+ h）/, '') })
  }
  const rep = repairTask(ev, tasks)
  if (rep) rows.push({ who: '現場人員', what: `${repairWhat(rep)}（核准後 Agent Nemo 派當下有空的人）` })
  if (chosen) rows.push({ who: chosen.who ?? (chosen.teams.join('、') || '人力'), what: `方案 ${chosen.opt.id}（${chosen.opt.label}）${chosen.shift ? `・${chosen.shift}` : ''}` })
  for (const n of after) {
    const step = ev?.steps.find((s) => s.step.id === n.id)?.step
    const teams = Array.isArray(step?.args.teams) ? (step!.args.teams as string[]).map((t) => TEAM_LABELS[t as keyof typeof TEAM_LABELS] ?? t) : []
    rows.push({ who: '系統', what: n.tool === 'notify_teams' && teams.length ? `通知${teams.join('、')}` : `${TOOL_LABEL[n.tool]}・${plain(n.purpose)}` })
  }
  return rows
}

/** A re-issued card (已重算) in one sentence (veteran 10-22): 「10 號硫化機的排程先生效了，3 號硫化機這張已依新排程重算：
 *  4 張訂單換機台，人力照你上次選的方案 B。請再核准一次。」 The per-order moves stay as the folded detail. Built from the
 *  card's code-built change lines (warroom/agent/state_machine.py card changes); a line it does not know is detail. */
export function changesLine(req: ApprovalRequest): { line: string; moves: string[] } {
  let head = '已照你說的重算'
  const moves: string[] = []
  const tail: string[] = []
  const orders = new Set<string>()
  for (const c of req.changes ?? []) {
    let rest = c
    const re = /^(.+?)排程先生效；(.+?)依現行排程重算：(.*)$/.exec(c)
    if (re) {
      const name = (s: string) => { const n = /^(\d+) 號機?$/.exec(s.trim()); return n ? `${n[1]} 號硫化機` : plain(s) }
      head = `${name(re[1])}的排程先生效了，${name(re[2])}這張已依新排程重算`
      rest = re[3]
    }
    const staff = /^人力沿用主管上次的選擇：\s*([A-Z])/.exec(rest)
    if (staff) { tail.push(`人力照你上次選的方案 ${staff[1]}`); continue }
    const rec = /^建議方案：.*→\s*([A-Z])/.exec(rest)
    if (rec) { tail.push(`系統建議改成方案 ${rec[1]}`); continue }
    for (const part of rest.split('；').map((s) => s.trim()).filter(Boolean)) {
      const mv = /^(#?\p{Lu}+-\d+)：/u.exec(part)
      if (mv) { orders.add(mv[1]); moves.push(plain(part)) } else if (!/共 \d+ 模改機台$/.test(part)) moves.push(plain(part))
    }
  }
  const what = [orders.size ? `${orders.size} 張訂單換機台` : '排程內容相同', ...tail].join('，')
  return { line: `${head}：${what}。請再核准一次。`, moves }
}
