// 核准卡「怎麼選」(Kyle 10-21: 「每一個理由/解決方案都相同，我們不知道勾哪一個才是正確，也不知道哪一個才能解除異常」).
// The A/B options on a card are staffing choices for the new schedule (warroom/schedule/staffing.py): they decide
// who covers the shift, not how the anomaly is fixed. This says so, names what does fix it, and why the system
// recommends one. Trade-offs follow SOP night-shift-staffing (A 夜班加班 R2 低風險 / B 跨組調人 R3 高風險).
import type { ApprovalOption, ApprovalRequest, Event } from './contracts'
import { cureChange, optionViews, type OptionView } from './agent/approvalView'
import { expectationOf, targetText } from './aiStatus'
import { machineLabel } from './factory'
import { plain } from './plain'

const STAFF_KINDS = new Set(['overtime', 'staff_move'])
const isStaffing = (o: ApprovalOption) => STAFF_KINDS.has(String(o.args?.kind ?? ''))
const STAFF_WORDS = /加班|調\s*\d*\s*人|調派|人力|補位/
const SCHEDULE_WORDS = /排程/

const TRADE_OFF: Record<string, string> = {
  overtime: '原班延長或同組加班，不影響其他產線（風險較低）',
  staff_move: '跨組調人，被調出的產線會少人（風險較高，核准要附理由）',
}
const RISK_RANK: Record<string, number> = { overtime: 0, staff_move: 1 }
/** 「7 號機」 for a press (as the card's actions say it), 「B 線開煉機」 otherwise */
const short = (id: string) => (id.startsWith('CURE-') ? plain(id) : machineLabel(id))

export interface OptionGuide { head: string; fix: string | null; lines: { id: string; text: string }[]; rec: string | null }

/** The remedy in one line: the machine taken out (round 2), the cure extension (round 1), else the first action that is
 *  neither staffing nor the schedule dump (「排程（第 2 版）生效（…）」 is the consequence, not the fix). */
function remedy(a: ApprovalRequest, ev: Event | null): string | null {
  const x = expectationOf(ev)
  const out = ev?.plan?.steps.find((s) => s.tool === 'notify_teams' && s.args.kind === 'machine_out')
  const outId = typeof out?.args.machine_id === 'string' ? out.args.machine_id : out ? ev?.machine_id : null
  if (outId) return `${short(outId)}停用交維修，其他模次改由別台`
  const cure = cureChange(ev)
  if (cure) {
    const check = x ? `，約 ${x.within_min} 分鐘內驗證是否${x.op === '<=' ? '降到' : '回到'} ${targetText(x)}` : ''
    return `延長 ${short(cure.press)}硫化到 ${Math.round(cure.to * 10) / 10} h${check}`
  }
  const act = (a.actions ?? []).map(plain).find((s) => !STAFF_WORDS.test(s) && !SCHEDULE_WORDS.test(s)) ?? null
  return act || x ? `${act ?? '現場依 SOP 處置'}${x ? `，約 ${x.within_min} 分鐘內由系統自動驗證是否恢復` : ''}` : null
}

export function optionGuide(a: ApprovalRequest, ev: Event | null): OptionGuide | null {
  if (a.options.length < 2 || !a.options.every(isStaffing)) return null
  const rec = a.options.find((o) => o.recommended)
  // the solver picks the recommended option; the card has one delivery_impact for every option, so only the risk differs
  const rank = (o: ApprovalOption) => RISK_RANK[String(o.args.kind)] ?? 0
  const safer = rec ? a.options.find((o) => rank(o) < rank(rec)) : undefined
  return {
    head: `${a.options.map((o) => o.id).join('、')} 只決定由誰補人力，選哪個都會照同一個計劃解除異常。`,
    fix: remedy(a, ev),
    lines: a.options.map((o) => ({ id: o.id, text: `${o.label}：${TRADE_OFF[String(o.args.kind)] ?? ''}` })),
    rec: rec ? `系統建議 ${rec.id}：排程求解器挑的方案${safer ? `；交期相同，想降低風險可改選 ${safer.id}` : ''}` : null,
  }
}

/** The phone card's 方案 (veteran 10-22; Kyle 10-21: each option says in one line what it changes, and why the system
 *  suggests it). Only when the card offers 2+ staffing choices; the 「怎麼選」 note above stays on the dashboard card.
 *  Why: the solver picks a transfer when it is allowed (no overtime cost, warroom/schedule/solver.py) and every option
 *  on a card has the same delivery verdict; a supervisor's preference (討論) or the choice carried over to a recomputed
 *  card wins over that. */
export interface OptionChoice { id: string; label: string; recommended: boolean; effect: string; why: string | null }
export function optionChoices(a: ApprovalRequest, ev: Event | null): OptionChoice[] {
  const staff = optionViews(a, ev).map((v) => ({ v, kind: staffKind(v) })).filter((x) => x.kind)
  if (staff.length < 2) return []
  const asked = (ev?.constraints ?? []).some((c) => ['prefer_option', 'no_overtime', 'no_transfer'].includes(c.kind))
  const kept = (a.changes ?? []).some((c) => c.startsWith('人力沿用主管上次的選擇'))
  return staff.map(({ v, kind }) => {
    const others = staff.filter((x) => x.v.opt.id !== v.opt.id).map((x) => x.v.opt.id).join('、')
    const from = v.who?.split(' → ')[0] ?? v.opt.label.match(/從\s*(.+?)調/)?.[1] ?? '被調出的組'
    const effect = kind === 'transfer'
      ? `${from}${v.shift ? ` ${v.shift}` : ''}少 ${v.people ?? 1} 人，可能影響產能・風險較高（跨組調人）`
      : '原班人員加班，不從別組調人'
    const why = !v.opt.recommended ? null
      : asked ? '照你說的' : kept ? '照你上次選的'
        : kind === 'transfer' ? `不用加班，交期與 ${others} 相同` : '不從別組調人，風險較低'
    return { id: v.opt.id, label: plain(v.opt.label), recommended: v.opt.recommended, effect, why }
  })
}
/** transfer | overtime | null: the solver's staffing kind, the option's notify kind, or its label. */
function staffKind(v: OptionView): 'transfer' | 'overtime' | null {
  const raw = String(v.kind ?? v.opt.args.kind ?? '')
  if (raw === 'transfer' || raw === 'staff_move') return 'transfer'
  if (raw === 'overtime') return 'overtime'
  return /調\s*\d*\s*人|跨組/.test(v.opt.label) ? 'transfer' : /加班/.test(v.opt.label) ? 'overtime' : null
}
