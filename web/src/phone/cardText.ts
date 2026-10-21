// The supervisor phone's words (veteran 10-22, 人力排程 v2 layout: the answer first, one sentence, then what 核准 does).
// Card:      請核准・03:54 前 → 「7 號硫化機溫度降到 122°C」 → 「疑似蒸汽壓不足。核准後 Agent Nemo 派人進廠維修，約 12 秒；溫度
//            回到正常才轉綠。」 → 核准後會做 (3 lines at most) → the 方案 only when there are 2+ staffing choices.
// Watching:  one line per open event, 「10 號硫化機・維修中・約 4 秒」 (no codes, no R0–R3).
// Pure (no React, no fetch): built only from the event, its card, the task board and /api/health repair_fast_s; the
// repair seconds are never written into the text by hand (本機 3 秒, 展示機 12 秒).
import type { ApprovalRequest, Event, WorkTask } from '../contracts'
import { CH_LABEL, expectationOf, readingText, recoverWord } from '../aiStatus'
import { cureChange } from '../agent/approvalView'
import { machineLabel } from '../factory'
import { fmtHM, fmtMD } from '../format'
import { plain } from '../plain'
import { type FastRepair, isRepairTask, secText, taskLeftS } from './fastEta'

const isMachineEvent = (ev: Event) => ev.source !== 'plant' && ev.source !== 'supervisor' && ev.machine_id !== 'PLANT'
const round1 = (n: number) => Math.round(n * 10) / 10
const num = (s: number) => String(round1(s))

/** 「溫度降到 122°C」 for a drop the detector measured, else the card's own title without 「異常：」 and the machine
 *  (「B 線開煉機 溫度過高」 → 「溫度過高」). */
export function problemText(ev: Event | null, req: ApprovalRequest | null = null): string {
  const title = plain(req?.title ?? ev?.alert_card?.title ?? ev?.anomaly.message ?? '').replace(/^異常[：:]\s*/, '')
  if (!ev || !isMachineEvent(ev)) return title
  const a = ev.anomaly
  if (a && a.channel in CH_LABEL && a.baseline > 0 && a.value < a.baseline) {
    const v = a.channel === 'temp_c' ? `${Math.round(a.value)}°C` : readingText(a.channel, a.value)
    return `${CH_LABEL[a.channel]}降到 ${v}`
  }
  const name = machineLabel(ev.machine_id)
  return title.replace(name, '').replace(/^\d+\s*號(?:硫化)?機\s*/, '').trim() || title
}

/** The card's big title: 「7 號硫化機溫度降到 122°C」 (a plant event or a command keeps its card title). */
export function cardTitle(ev: Event | null, req: ApprovalRequest): string {
  if (!ev || !isMachineEvent(ev)) return plain(req.title)
  return `${machineLabel(ev.machine_id)}${problemText(ev, req)}`
}

/** What the crew does once approved: 放慢投料 / 暫停投料 for a mill (the card's option or plan says so), else 進廠維修. */
function fieldVerb(ev: Event, req: ApprovalRequest | null): string {
  const kinds = [...(req?.options ?? []).map((o) => o.args?.kind), ...(ev.plan?.steps ?? []).map((s) => s.args?.kind)]
  return kinds.includes('slow_feed') ? '放慢投料' : kinds.includes('pause_feed') ? '暫停投料' : '進廠維修'
}

/** Does approving send someone to fix the machine? A repair task of this event (held 待派 until the decision), or the
 *  plan's repair notice. */
function repairs(ev: Event, req: ApprovalRequest | null, tasks: WorkTask[]): boolean {
  if (!isMachineEvent(ev)) return false
  if (tasks.some((t) => t.event_id === ev.id && isRepairTask(t))) return true
  const kinds = [...(req?.options ?? []).map((o) => o.args?.kind), ...(ev.plan?.steps ?? []).map((s) => s.args?.kind)]
  return kinds.some((k) => ['maintenance', 'machine_out', 'slow_feed', 'pause_feed'].includes(String(k)))
}

/** 「疑似蒸汽壓不足」: the cause Agent Nemo currently holds, its first clause (「蒸汽壓不足，溫度會穩定」 → 蒸汽壓不足). */
function cause(ev: Event): string | null {
  const h = ev.plan?.hypothesis ?? [...ev.hypotheses].reverse().find((x) => x.status !== 'rejected')
  const s = plain(h?.statement).split(/[，,]/)[0].trim()
  return s ? `${h?.status === 'confirmed' ? '' : '疑似'}${s}` : null
}

/** The sentence under the title: why, what 核准 does and how long, when it turns green. A plant event or a command says
 *  its reason instead. */
export function cardSentence(ev: Event | null, req: ApprovalRequest, tasks: WorkTask[], fast: FastRepair | null): string {
  if (!ev || !isMachineEvent(ev)) return plain(req.reason)
  const x = expectationOf(ev)
  const why = cause(ev)
  const act = repairs(ev, req, tasks) ? `派人${fieldVerb(ev, req)}${fast ? `，約 ${num(fast.repair_s)} 秒` : ''}` : '照下面的計畫處理'
  return `${why ? `${why}。` : ''}核准後 Agent Nemo ${act}；${x ? CH_LABEL[x.channel] : '讀值'}回到正常才轉綠。`
}

/** 「檢查蒸汽供應與溫度計」 from 「硫化機：7 號機溫度下降，請檢查蒸汽供應與溫度計」 (the task title, not hard-coded). */
export function taskWhat(title: string): string {
  const t = plain(title)
  const i = t.lastIndexOf('請')
  return i >= 0 ? t.slice(i + 1) : t.split('：').at(-1) ?? t
}

/** 「訂單交期不受影響」 / 「預估延誤：…」 / 「排程不變」 from the card's delivery verdict. */
function deliveryLine(text: string): string | null {
  const t = plain(text).trim()
  if (!t) return null
  if (t.startsWith('不受影響')) return '訂單交期不受影響'
  if (t.startsWith('延誤')) return `預估${t}`
  if (t.startsWith('排程未變更')) return '排程不變'
  return t.split('（')[0]
}

/** 核准後會做, 3 lines at most: who goes to fix what (the repair task's own title) and for how long, the cure change,
 *  the delivery verdict. Everything else (the full schedule line, the order moves) stays in 看依據. */
export function doLines(ev: Event | null, req: ApprovalRequest, tasks: WorkTask[], fast: FastRepair | null): string[] {
  const acts = (req.actions ?? []).filter((a) => !a.startsWith('人力：')).map(plain)
  // who goes to do what: the event's repair tasks (held 待派 until the decision), then the card's own field actions (a
  // mill's 「…，請放慢生料投入直到冷卻恢復」); the seconds once, on the first
  const fix = ev ? tasks.filter((t) => t.event_id === ev.id && isRepairTask(t) && t.status !== '完成') : []
  const out = [...fix.map((t) => `派人${taskWhat(t.title)}`),
    ...acts.filter((a) => !/排程|硫化總時數/.test(a) && a.includes('請')).map((a) => `派人${taskWhat(a)}`)]
  if (ev && !out.length && repairs(ev, req, tasks)) out.push(`派人${fieldVerb(ev, req)}`)
  if (out.length && fast) out[0] += `（約 ${num(fast.repair_s)} 秒）`
  const cure = cureChange(ev)
  if (cure) out.push(`這一模硫化延長到 ${round1(cure.to)} 小時（公式計算）${acts.some((a) => a.includes('無機台異動')) ? '，不換機台' : ''}`)
  const d = deliveryLine(req.delivery_impact)
  if (d) out.push(d)
  return [...new Set(out)].slice(0, 3)
}

/** The staffing gap this card leaves open, when no 方案 on it covers it: 「這次核准不含人力：10/03 夜班仍缺 1 人」. */
export function gapNote(req: ApprovalRequest, staffingChoice: boolean): string | null {
  if (staffingChoice) return null
  const left = (req.actions ?? []).find((a) => a.startsWith('人力：本次不含人力'))
  if (left) {
    const shift = left.match(/（(\d{1,2}\/\d{1,2}\s*\S+?班)/)?.[1]
    const n = left.match(/調\s*(\d+)\s*人/)?.[1]
    return `這次核准不含人力：${shift && n ? `${shift}仍缺 ${n} 人` : plain(left.slice(3))}`
  }
  const g = (req.gaps ?? []).map(plain).filter(Boolean)
  return g.length ? `這次核准不含人力：${g.map((x) => x.replace('缺', '仍缺')).join('、')}` : null
}

/** 「03:54 前」, with the date only when it is not today's (factory clock). */
export function deadlineText(expires: string | null, now: string | null): string | null {
  if (!expires) return null
  return `${now && fmtMD(now) !== fmtMD(expires) ? `${fmtMD(expires)} ` : ''}${fmtHM(expires)} 前`
}

/** A card carried over after another event's schedule took effect first (concurrent_3): 「10 號硫化機的排程先生效了，3 號
 *  硫化機這張已依新排程重算：4 張訂單換機台，人力照你上次選的方案 B。請再核准一次。」; null for a card the supervisor
 *  changed by 討論 (its changes are listed as they are). */
export function rebasedText(ev: Event | null, req: ApprovalRequest): string | null {
  const [head, ...rest] = req.changes ?? []
  const first = head?.match(/^(.+?)排程先生效；/)?.[1]
  if (!first) return null
  const orders = new Set((head.split('：').slice(1).join('：')).split('；').map((s) => s.match(/^([#\w-]+)[：\s]/)?.[1]).filter(Boolean))
  const keep = rest.map((s) => s.match(/^人力沿用主管上次的選擇：(\w+)/)?.[1]).find(Boolean)
  const name = ev ? machineLabel(ev.machine_id) : '這件'
  const what = [orders.size ? `${orders.size} 張訂單換機台` : '', keep ? `人力照你上次選的方案 ${keep}` : ''].filter(Boolean)
  // the server names a press short (「10 號機」); the phone says 「10 號硫化機」 like everywhere else on it
  const who = first.replace(/的$/, '').replace(/^(\d+)\s*號機$/, '$1 號硫化機')
  return `${who}的排程先生效了，${name}這張已依新排程重算${what.length ? `：${what.join('，')}` : ''}。請再核准一次。`
}

/** One open event, one line: 「10 號硫化機・維修中・約 4 秒」, 「B 線開煉機・放慢投料中・約 8 秒」, 「3 號硫化機・Agent Nemo 擬方案中」.
 *  `who`, for the single-event headline: 「黃建宏正在檢查蒸汽供應與溫度計。溫度回到正常才轉綠。」 (names from the task board). */
export interface LiveLine { id: string; name: string; phase: string; secs: string | null; who: string | null }
const FEED: Record<string, string> = { 放慢投料: '放慢投料中', 暫停投料: '暫停投料中' }
export function liveLine(ev: Event, tasks: WorkTask[], fast: FastRepair | null): LiveLine {
  const name = machineLabel(ev.machine_id)
  const mine = tasks.filter((t) => t.event_id === ev.id)
  const fix = mine.filter((t) => isRepairTask(t) && t.status !== '完成')
  const base = { id: ev.id, name, secs: null, who: null }
  const x = expectationOf(ev)
  if (ev.state === 'safe_stop') return { ...base, phase: '已安全停止' }
  if (fix.length && ev.state !== 'await_approval' && !['detected', 'classify', 'plan', 'simulate', 'gate'].includes(ev.state)) {
    const lead = fix.find((t) => t.worker_name) ?? fix[0]
    const feed = fix.map((t) => FEED[t.kind]).find(Boolean)
    return {
      ...base, phase: feed ?? '維修中',
      secs: fast ? secText(Math.max(...fix.map((t) => taskLeftS(t, fast)))) : null,
      who: `${lead.worker_name ? `${lead.worker_name}正在${taskWhat(lead.title)}` : `${taskWhat(lead.title)}，等人接手`}。`
        + `${x ? CH_LABEL[x.channel] : '讀值'}回到正常才轉綠。`,
    }
  }
  if (ev.state === 'closed' || ev.recovered_at) return { ...base, phase: '已恢復正常' }
  if (ev.repaired_at) return { ...base, phase: `維修完成・${recoverWord(x)}` }
  if (ev.state === 'await_approval') return { ...base, phase: '待核准' }
  if (ev.state === 'detected' || ev.state === 'classify') return { ...base, phase: 'Agent Nemo 分析中' }
  if (ev.state === 'execute' || ev.state === 'verify') {
    const open = mine.filter((t) => t.status !== '完成')
    return { ...base, phase: open.length ? `現場工作中（${mine.length - open.length}/${mine.length}）` : '確認讀值中' }
  }
  return { ...base, phase: 'Agent Nemo 擬方案中' }
}
