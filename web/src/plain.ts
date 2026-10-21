// Plain-language display text for judges and supervisors: step ids, hypothesis ids, press ids and
// version numbers become words. Display only — the data, the audit log and the timeline keep the codes.
import type { Event, RiskLevel } from './contracts'

const CN = ['零', '一', '二', '三', '四', '五', '六', '七', '八', '九']

/** 風險分級 (page 7) in the supervisor's words, the one table for every screen: 「自動」 or 「請核准」 (the phone fills in
 *  the reason itself, so no 「附理由」). The code R0–R3 stays on the Agent page, 狀態機 and 後台 only: RISK_LABEL there. */
export const RISK_PLAIN: Record<RiskLevel, string> = { R0: '只讀取', R1: '自動', R2: '請核准', R3: '請核准' }
/** "R3" → "R3・請核准", for the screens that keep the code. */
export const RISK_LABEL = Object.fromEntries(Object.entries(RISK_PLAIN).map(([r, t]) => [r, `${r}・${t}`])) as Record<RiskLevel, string>

/** "H1" → "判斷一" */
export function hypName(id: string): string {
  const n = Number(id.replace(/^H/, ''))
  return Number.isInteger(n) && n > 0 && n < 10 ? `判\u2060斷\u2060${CN[n]}` : id  // word joiners: never break 判斷|二
}

export function plain(s: string | null | undefined): string {
  if (!s) return ''
  return s
    .replace(/CURE-0?(\d+)/g, '$1 號機')
    .replace(/(^|[；;，,、\s（(→])S\d+b?\s+/g, '$1')
    .replace(/假設\s*H(\d)/g, (_, n) => hypName(`H${n}`))
    .replace(/\bH(\d)\b\s?/g, (_, n) => hypName(`H${n}`))
    .replace(/排程\s*v(\d+)\s*/g, '第 $1 版排程')
    .replace(/\bv(\d+)\b/g, '第 $1 版')
    .replace(/求解：/g, '重排：')
}

/** After the event closes, the headline should not still shout 異常. */
export function closedTitle(title: string, closed: boolean): string {
  return closed ? title.replace(/^異常：/, '已處置：') : title
}

/** A hypothesis never tested by a reading stays 驗證中 only while the event is open. On close it is
 *  handed to maintenance when the event took a machine out or called maintenance (page 11 H2). */
export function hypLabel(status: string, ev: Event): string {
  if (status === 'rejected') return '不成立'
  if (status === 'confirmed') return '成立'
  if (ev.state !== 'closed' && ev.state !== 'safe_stop') return '驗證中'
  const all = [...(ev.prior_rounds ?? []).flatMap((r) => r.steps), ...ev.steps]
  const handed = all.some((s) => s.state === 'ok' && s.step.tool === 'notify_teams' && ['machine_out', 'maintenance'].includes(String(s.step.args.kind)))
  return handed ? '未驗證・交維修' : '未驗證'
}
