// 主管摘要 (supervisor briefing): the short, sectioned text a supervisor reads on the phone card and in the desktop
// Agent panel. Built ONLY from data the server already sends (the event, its approval card, the world's task board,
// the machine tiles); nothing here is hard-coded or invented. A missing optional field leaves its line out; a
// missing required field reads 待確認. Display only: the approval, the tools and the audit log are untouched.
//
// Three states (CJ, 2026-10-21):  待核准 · 執行中 · 處置完成  (a safe stop is its own state, 已安全停止, because
// it must never read as a finished fix).  Wording rules the builder keeps:
//   - 「已核准」 is not 「已執行」; 「維修已通知」 is not 「設備已修復」: schedule done and equipment status are separate.
//   - a delivery verdict is 預估 (solver) unless a real outcome exists; an unresolved gap is always listed.
//   - a cause is 疑似 until its hypothesis is confirmed by a reading.
import {
  WRITE_TOOLS, type ApprovalOption, type ApprovalRequest, type Event, type MachineStatus, type MachineTile, type StepRun, type WorkTask,
} from './contracts'
import { machineLabel } from './factory'
import { fmtHM, fmtMD } from './format'
import { hypLabel, plain } from './plain'
import { NOTIFY_LABEL, resultText, staffingChoice } from './stepText'
import { cureChange, deliveryFacts, optionViews, type OptionView } from './agent/approvalView'
import { expectationText, repairWindow } from './phone/RepairEta'

export type BriefState = 'pending' | 'running' | 'followup' | 'done' | 'stopped'
export type SectionKey = 'advice' | 'impact' | 'deadline' | 'progress' | 'blocker' | 'eta' | 'result' | 'equipment' | 'todo'

export interface BriefSection {
  key: SectionKey
  title: string           // bolded by the view (strong); the body keeps normal weight
  lines: string[]         // one point per line
  plain?: boolean         // true: render the lines as short paragraphs instead of bullets
}
export interface Briefing {
  state: BriefState
  stateLabel: string
  name: string            // 「7 號硫化機」「全廠」「主管指令」
  lead: string            // one sentence under the header
  sections: BriefSection[]
  /** 待核准 only: the 請核准 chip and its deadline (null time = no reliable deadline, the chip shows alone) */
  deadline: { time: string | null } | null
  /** collapsed under the summary (查看執行明細 / 查看處理結論): what the supervisor does not need to decide or watch */
  details?: { title: string; lines: string[] }
  eventId: string
}

export interface BriefCtx {
  ev: Event | null
  /** the card waiting for the supervisor (待核准) */
  req: ApprovalRequest | null
  /** the staffing option currently picked on the card (null = the system's recommendation) */
  chosen: string | null
  resolved: ApprovalRequest[]
  tasks: WorkTask[]
  machines: MachineTile[]
  now: string | null
  /** maintenance in progress, per machine id (world.maintenance.in_progress) */
  maintenance?: { machine_id: string; title: string; status: string }[]
}

const STATUS_PLAIN: Record<MachineStatus, string> = {
  running: '運轉中', idle: '待機', warning: '注意（讀值偏離）', alarm: '異常', stopped: '已停機', out_of_service: '停用中', offline: '離線',
}

const isMachineEvent = (ev: Event) => ev.source !== 'plant' && ev.source !== 'supervisor' && ev.machine_id !== 'PLANT'

export function eventName(ev: Event): string {
  if (ev.source === 'supervisor') return '主管指令'
  return machineLabel(ev.machine_id)
}

/** The card's recommended (or the picked) option, in words the supervisor can decide on. */
function optionText(o: OptionView | undefined, opt: ApprovalOption | undefined, kind: 'transfer' | 'overtime' | null): string {
  if (!o && !opt) return '待確認'
  if (o?.who && o.kind === 'transfer' && kind === 'transfer') return `從 ${o.who.split(' → ')[0]}調 ${o.people ?? 1} 人${o.shift ? `（${o.shift}）` : ''}`
  if (kind === 'overtime') return plain(opt?.label ?? '夜班加班')
  return plain(opt?.label ?? '待確認')
}

/** One option for the phone selector: the plan, who is affected, how risky. Shared by the briefing and the selector. */
export interface OptionBrief { id: string; label: string; recommended: boolean; effect: string[]; high: boolean; staffing: boolean }
/** transfer | overtime | null, from the solver's staffing change, the option's args (real backend: notify kind
 *  staff_move / overtime; mock: staffing transfer / overtime) or, last, the option's own label. */
function staffingKind(v: OptionView): 'transfer' | 'overtime' | null {
  const raw = String(v.kind ?? v.opt.args.staffing ?? '')
  if (raw === 'transfer' || raw === 'staff_move') return 'transfer'
  if (raw === 'overtime') return 'overtime'
  if (/調\s*\d*\s*人|跨組/.test(v.opt.label)) return 'transfer'
  if (/加班/.test(v.opt.label)) return 'overtime'
  return null
}

export function optionBriefs(req: ApprovalRequest, ev: Event | null): OptionBrief[] {
  return optionViews(req, ev).map((v) => {
    const kind = staffingKind(v)
    const effect: string[] = []
    if (kind === 'transfer') {
      const from = v.who ? v.who.split(' → ')[0] : '調出組（組別待確認）'
      effect.push(`${from}少 ${v.people ?? 1} 人${v.shift ? `（${v.shift}）` : ''}，可能影響該組產能`)
    } else if (kind === 'overtime') {
      effect.push(`${v.who ?? '原班人員加班'}${v.shift ? `・${v.shift}` : ''}，不從其他組調人`)
    }
    const label = optionText(v, v.opt, kind)
    return { id: v.opt.id, label, recommended: v.opt.recommended, effect, high: kind === 'transfer' && req.risk === 'R3', staffing: kind !== null }
  })
}

/** A long action line (a whole re-plan, a notice with its message) is cut to its first clause; the full text stays
 *  in 查看處置依據 (fullActionLines). */
export function shortAction(a: string): string {
  if (a.length <= 56) return a
  const colon = a.indexOf('：')
  if (colon >= 4) return a.slice(0, colon)
  const paren = a.indexOf('（', 6)
  return paren > 0 ? `${a.slice(0, paren)}（細節見處置依據）` : `${a.slice(0, 38)}…`
}
export function fullActionLines(req: ApprovalRequest): string[] {
  return (req.actions ?? []).filter((a) => !a.startsWith('人力：依所選')).map((a) => plain(a).replace(/）\s+生效/, '）生效'))
}

export function actionLines(req: ApprovalRequest): string[] {
  // 「排程生效」 and 「硫化總時數」 usually describe the same change: say it once (only when the hours match)
  const acts = (req.actions ?? []).filter((a) => !a.startsWith('人力：')).map((a) => plain(a).replace(/）\s+生效/, '）生效'))
  const sched = acts.find((a) => /排程.*生效/.test(a))
  return acts.filter((a) => {
    if (!sched || a === sched || !/硫化總時數/.test(a)) return true
    const h = a.match(/(\d+(?:\.\d+)?) h/)
    return !(h && sched.includes(`${h[1]} h`))
  }).map(shortAction)
}

function deliveryLine(text: string | null | undefined): string | null {
  const t = plain(text).trim()
  if (!t) return null
  if (t.startsWith('不受影響')) return `預估訂單交期不受影響${t.length > 4 ? t.slice(4) : ''}`
  if (t.startsWith('延誤')) return `預估${t}`
  return t
}

/** The solver's delivery verdict for an event, when the approval card itself is not at hand (page reloaded). */
function deliveryFromEvent(ev: Event): string | null {
  const f = deliveryFacts(ev)
  if (f.met === true) return '預估訂單交期不受影響'
  if (f.met === false) return `預估延誤：${f.late.join('、') || '有訂單'}`
  return null
}

function gapLines(req: ApprovalRequest | null | undefined): string[] {
  return (req?.gaps ?? []).map((g) => plain(g)).filter(Boolean)
}

/** What the commander currently believes caused a machine event; 疑似 until a reading confirmed it. */
function causeText(ev: Event): string | null {
  if (!isMachineEvent(ev)) return null
  const h = ev.plan?.hypothesis ?? [...ev.hypotheses].reverse().find((x) => x.status !== 'rejected')
  if (!h || !h.statement) return null
  return `${h.status === 'confirmed' ? '' : '疑似'}${plain(h.statement)}`
}

/** The header already names the machine, so the sentence under it does not repeat 「7 號機」/「7 號硫化機」. */
function withoutMachine(ev: Event, text: string): string {
  if (!isMachineEvent(ev)) return text
  const n = String(machineLabel(ev.machine_id)).match(/\d+/)?.[0]
  if (!n) return text
  return text.replace(new RegExp(`^${n}\\s*號(?:硫化)?機\\s*`), '本機')
}

function headline(ev: Event, req: ApprovalRequest | null): string {
  const what = withoutMachine(ev, plain(req?.title ?? ev.alert_card?.title ?? ev.anomaly.message))
  const cause = causeText(ev)
  if (isMachineEvent(ev)) return cause ? `${what}，${cause}。` : `${what}。`
  const why = plain(req?.reason ?? ev.anomaly.message)
  return what && why && what !== why ? `${what}：${why}。` : `${what || why}。`
}

const stepName = (sr: StepRun): string => {
  const a = sr.step.args
  try {
    return plain(resultText(sr.step.tool, a, sr.result?.data ?? {})).replace(/）\s+生效/, '）生效').replace(/保留\s*第/, '保留第')
  } catch { return NOTIFY_LABEL[String(a.kind)] ?? sr.step.tool }
}
const STEP_STATE: Record<StepRun['state'], string> = {
  ok: '已完成', running: '進行中', pending: '等待中', awaiting_approval: '等待核准', failed: '失敗', skipped: '未執行', rejected: '被攔下',
}
/** Gated writes of the current round (what the approval actually runs), oldest first. */
function writeSteps(ev: Event): StepRun[] {
  return ev.steps.filter((s) => WRITE_TOOLS.includes(s.step.tool))
}
const taskLine = (t: WorkTask): string =>
  `${plain(t.title)}：${t.status}${t.status !== '完成' && t.eta ? `，預計 ${fmtHM(t.eta)}` : ''}${t.status === '完成' && t.done_at ? `，${fmtHM(t.done_at)} 完成` : ''}`

function approvedOf(ev: Event, resolved: ApprovalRequest[]): ApprovalRequest | null {
  return resolved.filter((r) => r.event_id === ev.id && r.status === 'approved')
    .sort((a, b) => ((a.decision?.decided_at ?? a.created_at) < (b.decision?.decided_at ?? b.created_at) ? 1 : -1))[0] ?? null
}
function approvedLabel(req: ApprovalRequest, ev: Event): string {
  const id = req.decision?.option_id
  if (req.options.length > 1 && id) {
    const ob = optionBriefs(req, ev).find((o) => o.id === id)
    return `方案 ${id}：${ob?.label ?? plain(req.options.find((o) => o.id === id)?.label)}`
  }
  const acts = actionLines(req)
  return acts[0] ? `${acts[0]}` : plain(req.title)
}

/** 「方案 B 已核准：從 2 線調 1 人」: the approved plan, by name. */
function approvedLead(req: ApprovalRequest, ev: Event): string {
  const id = req.decision?.option_id
  if (req.options.length > 1 && id) {
    const ob = optionBriefs(req, ev).find((o) => o.id === id)
    return `方案 ${id} 已核准：${ob?.label ?? plain(req.options.find((o) => o.id === id)?.label)}。`
  }
  return `${approvedLabel(req, ev)}已核准。`
}

function equipmentLine(ev: Event, c: BriefCtx): string | null {
  if (!isMachineEvent(ev)) return null
  const m = c.machines.find((t) => t.machine_id === ev.machine_id)
  const fix = c.maintenance?.find((x) => x.machine_id === ev.machine_id)
  const parts: string[] = []
  if (m) parts.push(`${machineLabel(ev.machine_id)}${STATUS_PLAIN[m.status]}${m.temp_c != null ? `（${Math.round(m.temp_c * 10) / 10}°C）` : ''}`)
  if (fix) { const t = fix.title.split('：'); const short = t.length > 1 ? t[1] : t[0]; parts.push(fix.status === 'in_progress' ? `維修進行中：${short}` : `維修已排入：${short}`) }
  else if (hypLabel('active', ev) === '未驗證・交維修') parts.push('已交維修，尚未回報修復')
  return parts.length ? parts.join('；') : '待確認'
}

export function buildBriefing(c: BriefCtx): Briefing | null {
  const { ev, req } = c
  if (!ev) return null
  const name = eventName(ev)
  const base = { name, eventId: ev.id }

  // ------------------------------------------------------------ 待核准
  if (req) {
    const opts = optionBriefs(req, ev)
    const rec = opts.find((o) => o.recommended) ?? opts[0]
    const pick = opts.find((o) => o.id === (c.chosen ?? rec?.id)) ?? rec
    const advice: string[] = []
    if (pick) {
      const prefix = opts.length > 1 ? `方案 ${pick.id}：` : ''
      advice.push(pick.id === rec?.id ? `${prefix}${pick.label}` : `${prefix}${pick.label}（你改選；系統建議方案 ${rec?.id}）`)
    }
    advice.push(...actionLines(req))
    // 交期與風險: delivery verdict, the picked staffing option's cost, then what is still unresolved
    const impact: string[] = []
    const d = deliveryLine(req.delivery_impact)
    if (d) impact.push(d)
    if (pick?.effect.length) impact.push(...pick.effect.map((e) => `人力：${e}`))
    // a staffing action from an earlier round ("第 1 輪已核准並執行…本輪改選會撤回") is a risk, not part of this card's scope
    impact.push(...(req.actions ?? []).filter((x) => x.startsWith('人力：') && !x.startsWith('人力：依所選')).map((x) => `注意：${plain(x).slice(3)}`))
    const gaps = gapLines(req)
    impact.push(...gaps.map((g) => `未解決缺口：${g}`))
    const cure = cureChange(ev)
    if (cure) impact.push(`${machineLabel(cure.press)}硫化時間由 ${round1(cure.from)} h 延長為 ${round1(cure.to)} h`)
    const sections: BriefSection[] = []
    if (advice.length) sections.push({ key: 'advice', title: '建議處置', lines: advice })
    if (impact.length) sections.push({ key: 'impact', title: '交期與風險', lines: impact })
    return {
      ...base, state: 'pending', stateLabel: '待核准', lead: headline(ev, req), sections,
      deadline: { time: req.expires_at ? fmtHM(req.expires_at) : null },
    }
  }

  // ------------------------------------------------------------ 已安全停止
  if (ev.state === 'safe_stop') {
    return {
      ...base, state: 'stopped', stateLabel: '已安全停止', deadline: null,
      lead: `${plain(ev.alert_card?.title ?? ev.anomaly.message)}：自動處置未能完成，已安全停止、保留原排程。`,
      sections: [
        { key: 'result', title: '處理結果', lines: [plain(ev.outcome) || '待確認'] },
        { key: 'todo', title: '後續待辦', lines: ['需要人工判斷與處置'] },
      ],
    }
  }

  const approved = approvedOf(ev, c.resolved)

  const evTasks = c.tasks.filter((t) => t.event_id === ev.id)
  const openTasks = evTasks.filter((t) => t.status !== '完成')
  const fixing = isMachineEvent(ev) && !!c.maintenance?.some((m) => m.machine_id === ev.machine_id && m.status === 'in_progress')
  const dl = approved ? deliveryLine(approved.delivery_impact) : deliveryFromEvent(ev)
  const staffed = !!approved && approved.options.length > 1 && !!approved.decision?.option_id
  /** 交期與風險: the delivery verdict (預估), the cost of the approved staffing option, and whatever is still open */
  const riskLines = (): string[] => {
    const out: string[] = []
    if (dl) out.push(dl)
    if (approved && staffed) {
      const ob = optionBriefs(approved, ev).find((o) => o.id === approved.decision?.option_id)
      if (ob?.effect.length) out.push(...ob.effect.map((e) => `人力：${e}`))
    } else if (approved) out.push(...gapLines(approved).map((g) => `未解決缺口：${g}`))
    return out
  }
  const writes = writeSteps(ev)
  const execLines = [...new Set(writes.filter((s) => s.state === 'ok').map(stepName))]

  // ------------------------------------------------------------ 處置完成 / 排程已更新・現場處理中
  if (ev.state === 'closed') {
    const followup = openTasks.length > 0 || fixing
    const result = execLines.map(shortAction)
    // 已核准 ≠ 已執行: while the staffing move is still a field task it is listed under 現場待辦, not 已完成
    if (approved && staffed && !followup) {
      const t = `人力：${approvedLabel(approved, ev)}`
      if (!result.some((r) => r.includes(approved.decision!.option_id ?? '~'))) result.push(t)
    }
    const eq = equipmentLine(ev, c)
    const concl = plain(ev.outcome).replace(/\u2060/g, '').replace(/\s+(?=生效)/g, '')
    const scheduled = execLines.some((l) => /排程.*生效/.test(l))
    const sections: BriefSection[] = [{ key: 'result', title: followup ? '已完成' : '處理結果', lines: result.length ? result : [concl || '待確認'] }]
    if (followup && openTasks.length) sections.push({ key: 'todo', title: '現場待辦', lines: openTasks.map(taskLine) })
    if (eq) sections.push({ key: 'equipment', title: '設備狀態', lines: [eq], plain: true })
    const risk = riskLines()
    if (risk.length) sections.push({ key: 'impact', title: '交期與風險', lines: risk })
    const details = concl ? { title: '查看處理結論', lines: [concl] } : undefined
    if (followup) {
      const doneN = evTasks.length - openTasks.length
      return {
        ...base, state: 'followup', stateLabel: scheduled ? '排程已更新｜現場處理中' : '現場處理中', deadline: null, details, sections,
        lead: evTasks.length ? `現場工作尚未全部完成（${doneN}/${evTasks.length}），設備尚未確認修復。` : '現場維修仍在進行，設備尚未確認修復。',
      }
    }
    const did = result.slice(0, 3)
    return { ...base, state: 'done', stateLabel: '處置完成', lead: did.length ? `已完成：${did.join('、')}。` : (concl ? `${concl}。` : '處置流程已結案。'), sections, details, deadline: null }
  }

  // ------------------------------------------------------------ 執行中 (approved, not closed yet)
  // the client only holds the approval record for decisions it saw; after a reload the event itself says it ran
  const ran = !!approved || ev.state === 'execute' || ev.state === 'verify' || (ev.state === 'replan' && ev.approvals.length > 0)
  if (ran) {
    const okN = writes.filter((s) => s.state === 'ok').length
    const progress: string[] = []
    if (writes.length) progress.push(`系統動作 ${okN}/${writes.length} 完成`)
    if (evTasks.length) progress.push(`現場任務 ${evTasks.length - openTasks.length}/${evTasks.length} 完成`)
    if (ev.state === 'replan') progress.push('驗證未達預期：正在換假設重新規劃')
    else if (ev.state === 'verify') progress.push('動作完成後，系統正依讀值驗證是否達到預期')
    // 目前卡點: a step that failed / is stopped, else the field task that is waiting longest
    const stuck = writes.find((s) => s.state === 'failed' || s.state === 'rejected' || s.state === 'awaiting_approval')
    const nextTask = [...openTasks].sort((x, y) => String(x.eta ?? '~').localeCompare(String(y.eta ?? '~')))[0]
    const blocker = stuck ? `${stepName(stuck)}：${STEP_STATE[stuck.state]}`
      : nextTask ? `${plain(nextTask.title)}：${nextTask.status}${nextTask.eta ? `，預計 ${fmtHM(nextTask.eta)}` : ''}` : null
    const w = repairWindow(ev, c.now, c.tasks, c.resolved)
    const eta = w.doneBy
      ? `${fmtMD(w.doneBy)} ${fmtHM(w.doneBy)}${w.lastEta ? '（含現場任務）' : w.x ? `（${expectationText(w.x)}）` : ''}`
      : '待確認'
    const sections: BriefSection[] = [{ key: 'progress', title: '實際進度', lines: progress.length ? progress : ['待確認'] }]
    if (blocker) sections.push({ key: 'blocker', title: '目前卡點', lines: [blocker], plain: true })
    sections.push({ key: 'eta', title: '預計完成', lines: [eta], plain: true })
    const risk = riskLines()
    if (risk.length) sections.push({ key: 'impact', title: '交期與風險', lines: risk })
    const more = [...writes.map((s) => `${stepName(s)}：${STEP_STATE[s.state]}`), ...evTasks.map(taskLine)]
    return {
      ...base, state: 'running', stateLabel: '執行中', sections, deadline: null,
      details: more.length ? { title: '查看執行明細', lines: more } : undefined,
      lead: approved ? approvedLead(approved, ev) : (staffingChoice(ev.steps) ? `已核准：${staffingChoice(ev.steps)}。` : '已核准。'),
    }
  }
  return null   // still planning: the existing 「AI 指揮官…中」 line covers it
}

const round1 = (n: number) => Math.round(n * 10) / 10

