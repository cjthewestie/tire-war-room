// 核准卡「怎麼選」(Kyle 10-21: 「每一個理由/解決方案都相同，我們不知道勾哪一個才是正確，也不知道哪一個才能解除異常」).
// The A/B options on a card are staffing choices for the new schedule (warroom/schedule/staffing.py): they decide
// who covers the shift, not how the anomaly is fixed. This says so, names what does fix it, and why the system
// recommends one. Trade-offs follow SOP night-shift-staffing (A 夜班加班 R2 低風險 / B 跨組調人 R3 高風險).
import type { ApprovalOption, ApprovalRequest, Event } from './contracts'
import { expectationOf } from './aiStatus'
import { plain } from './plain'

const STAFF_KINDS = new Set(['overtime', 'staff_move'])
const isStaffing = (o: ApprovalOption) => STAFF_KINDS.has(String(o.args?.kind ?? ''))
const STAFF_WORDS = /加班|調\s*\d*\s*人|調派|人力|補位/

const TRADE_OFF: Record<string, string> = {
  overtime: '原班延長或同組加班，不影響其他產線（風險較低）',
  staff_move: '跨組調人，被調出的產線會少人（風險較高，核准要附理由）',
}

export interface OptionGuide { head: string; fix: string | null; lines: { id: string; text: string }[]; rec: string | null }

export function optionGuide(a: ApprovalRequest, ev: Event | null): OptionGuide | null {
  if (a.options.length < 2 || !a.options.every(isStaffing)) return null
  const fixAct = (a.actions ?? []).map(plain).find((x) => !STAFF_WORDS.test(x)) ?? null
  const x = expectationOf(ev)
  const fix = fixAct || x ? `${fixAct ?? '現場依 SOP 處置'}${x ? `，約 ${x.within_min} 分鐘內由系統自動驗證是否恢復` : ''}` : null
  const rec = a.options.find((o) => o.recommended)
  return {
    head: `${a.options.map((o) => o.id).join('、')} 只決定由誰補人力，選哪個都會照同一個計劃解除異常。`,
    fix,
    lines: a.options.map((o) => ({ id: o.id, text: `${o.label}：${TRADE_OFF[String(o.args.kind)] ?? ''}` })),
    rec: rec ? `系統建議 ${rec.id}：排程計算挑了這個方案` : null,
  }
}
