// Issue #59: the incident report for one event, built by code from what the system recorded (the event's timeline,
// hypotheses, steps and safety actions, its approval cards, the worker tasks, the audit chain). No model writes it,
// so each line can be traced back to a record. The prevention measures are the one part a person writes. Agent Nemo's
// 異常報告 (Event.ai_report) sits on top of it on /report and goes into the copied text.
import type { AnomalyKind, ApprovalRequest, Event, EventState, StepRun, WorkTask } from '../contracts'
import { repairStage } from '../aiStatus'
import { STATE_LABEL } from '../components/common'
import { MACHINE_META, machineLabel } from '../factory'
import { fmtHM, fmtMD } from '../format'
import { hypLabel, hypName, plain } from '../plain'
import { TOOL_LABEL, resultText } from '../stepText'

export interface ReportInput { ev: Event; approvals: ApprovalRequest[]; tasks: WorkTask[]; chainOk: boolean | null; auditCount: number | null }
export interface Row { k?: string; time?: string; text: string; tone?: 'ok' | 'warn' | 'bad' }
export interface Part { id: string; title: string; rows: Row[]; empty?: string }
export interface Report { title: string; head: Row[]; ai: { tag: string; lines: string[] } | null; parts: Part[] }

const CHANNEL: Record<string, string> = { temp_c: '溫度', pressure_kgf_cm2: '壓力', water_flow_lpm: '水流', steam_kgf_cm2: '蒸汽壓' }
const UNIT: Record<string, string> = { temp_c: '°C', pressure_kgf_cm2: ' kgf/cm²', water_flow_lpm: ' L/min', steam_kgf_cm2: ' kgf/cm²' }
const DECISION: Record<string, string> = { approved: '核准', modified: '修改後核准', rejected: '否決', expired: '逾時未決', superseded: '被新版取代', pending: '待核准' }
export const VIA: Record<string, string> = { phone: '手機', touchscreen: '現場觸控螢幕', dashboard: '戰情室', auto: '自動' }
// the timeline's actor names on screen (the audit keeps 指揮官 / supervisor)
const ACTOR: Record<string, string> = { 指揮官: 'Agent Nemo', supervisor: '主管' }

/** Who decided: the supervisor's account reads 主管 (as in history.ts). */
export const who = (by: string | null | undefined) => (!by || by === 'supervisor' || by === 'dashboard' ? '主管' : by === 'system' ? '系統' : by)

/** plain() plus what the records still carry for engineers: the event id in front (八、稽核 gives it), the [工具] / [人工]
 *  tags, the solver's name, the approval id a carried-over approval names, 指揮官 (now Agent Nemo) and the older words
 *  for 安全把關 / 稽核紀錄 / 安全停止; 「排程 v3 生效」 reads 「第 3 版排程生效」 (glossary). Display only. */
export function tidy(s: string | null | undefined): string {
  return plain((s ?? '').replace(/^EV-[^｜\s]*｜/, '').replace(/沿用(核准)? AP-[\w-]+/g, '沿用原核准')
    .replace(/排程\s*v(\d+)\s*/g, '第 $1 版排程').replace(/([\u4e00-\u9fff])\s+v(\d+)\b/g, '$1第 $2 版'))
    .replace(/\[(工具|人工)\]\s*/g, '')
    .replace(/喚醒指揮官/g, '交給 Agent Nemo')
    .replace(/指揮官/g, 'Agent Nemo').replace(/([\u4e00-\u9fff])Agent Nemo/g, '$1 Agent Nemo').replace(/Agent Nemo([\u4e00-\u9fff])/g, 'Agent Nemo $1')
    .replace(/，?可行（CP-SAT）/g, '')
    .replace(/動作把關/g, '安全把關')
    .replace(/稽核日誌/g, '稽核紀錄')
    .replace(/安全中止/g, '安全停止')
}

/** STATE_LABEL in the supervisor's words: replan reads 重新判斷 (glossary), not 換假設. */
export const stateLabel = (state: string) => (state === 'replan' ? '重新判斷' : STATE_LABEL[state as EventState] ?? state)

/** Approved cards whose schedule went stale before it took effect (3 at once): a later version of the card replaced
 *  it (supersedes), or the re-solve carried the same content over as 「<id>-rebased」 (state_machine.py). The schedule
 *  such a card names never took effect. */
export function staleCards(approvals: ApprovalRequest[]): Set<string> {
  return new Set(approvals.flatMap((a) => [a.supersedes, /^(.+)-rebased\d*$/.exec(a.id)?.[1]]).filter((x): x is string => !!x))
}

/** 「10 號硫化機 溫度下降 128→122.4°C」「B 線開煉機 溫度過高」: the machine's full name and what went wrong, from a title
 *  (the alert card's, an approval's), not the detector's forecast sentence; without 「異常：」 / 「已處置：」 (the state is
 *  written next to it) and without the machine named twice. */
export function headline(machineId: string | null | undefined, title: string): string {
  const s = tidy(title.replace(/^(異常|已處置)[：:]\s*/, ''))
  const m = machineId ? MACHINE_META[machineId] : undefined
  if (!m || !machineId) return s
  const full = machineLabel(machineId)
  const short = `${m.short} 號機`
  const rest = s.startsWith(full) ? s.slice(full.length) : m.line === 'CURE' && s.startsWith(short) ? s.slice(short.length) : s
  return `${full} ${rest.replace(/^\s*[：:]/, '').trim()}`.trim()
}

const KIND: Partial<Record<AnomalyKind, string>> = {
  temp_drop: '溫度下降', overheat: '溫度過高', pressure_low: '壓力偏低', water_flow_low: '冷卻水流偏低', steam_low: '蒸汽壓偏低', schedule_slip: '排程延誤',
}
const num = (v: number) => String(Math.round(v * 10) / 10)
/** Before Agent Nemo writes the alert card (alert_card null): the same short title from the anomaly's fields, as
 *  commander.py writes it (「溫度下降 128→122.4°C」), not the detector's sentence that names the machine again. */
function anomalyTitle(ev: Event): string | null {
  const a = ev.anomaly
  if (!(ev.machine_id in MACHINE_META) || a.kind === 'supervisor_command') return null
  return a.kind === 'temp_drop' ? `${KIND.temp_drop} ${num(a.baseline)}→${num(a.value)}°C` : KIND[a.kind] ?? '異常'
}
export const eventTitle = (ev: Event) => headline(ev.machine_id, ev.alert_card?.title || ev.command || anomalyTitle(ev) || ev.anomaly.message || ev.goal || ev.id)

/** The result as the supervisor reads it. 「已恢復正常」 only when a reading confirmed it (outcome 已修復, as aiStatus.ts
 *  closedPhase): 「已修復：10 號機 溫度回到 128°C（維修完成 10:03、恢復 10:04）」 → 「已恢復正常，溫度回到 128°C」. A machine
 *  taken out or an unverified close says what really happened. */
export function outcomeText(machineId: string | null | undefined, state: string, outcome: string | null | undefined): string {
  const o = tidy(outcome)
  if (state === 'safe_stop') { const why = o.replace(/^安全停止[：:]\s*/, ''); return why ? `安全停止：${why}` : '安全停止' }
  if (state !== 'closed') return `處理中（${stateLabel(state)}）`
  if (/停用/.test(o)) return '已停用該機，排程已調整'
  if (/未驗證/.test(o)) return '已處置，未用讀值確認'
  if (!/^已修復[：:]/.test(o)) return o || '已結束'
  let r = o.replace(/^已修復[：:]\s*/, '').replace(/（維修完成[^）]*）$/, '')
  const m = machineId ? MACHINE_META[machineId] : undefined
  for (const name of m && machineId ? [machineLabel(machineId), `${m.short} 號機`] : []) if (r.startsWith(name)) r = r.slice(name.length)
  r = r.trim()
  return r ? `已恢復正常，${r}` : '已恢復正常'
}

/** An event still open, in the supervisor's words: 待核准 / 維修中 / 回溫中 (the fast-repair flow), else 處理中（state）. */
export function openPhase(ev: Event, tasks: WorkTask[]): string {
  if (ev.state === 'await_approval') return '待核准'
  const st = repairStage(ev, tasks)
  return st === 'repairing' ? '維修中' : st === 'recovering' ? '回溫中' : st === 'recovered' ? '已恢復正常' : `處理中（${stateLabel(ev.state)}）`
}

const t = (iso: string | null | undefined) => (iso ? Date.parse(iso) : NaN)
function dur(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '—'
  if (ms < 60000) return `${Math.max(1, Math.round(ms / 1000))} 秒`   // a fast repair at 1×: seconds, not 「0 分鐘」
  const m = Math.round(ms / 60000)
  return m < 60 ? `${m} 分鐘` : `${Math.floor(m / 60)} 小時 ${m % 60} 分`
}

export function buildReport({ ev, approvals, tasks, chainOk, auditCount }: ReportInput): Report {
  const closed = ev.state === 'closed' || ev.state === 'safe_stop'
  const title = eventTitle(ev)
  // one line (Kyle 10-21 flow): when it happened, the repair, back to normal, how long, the result
  // a later step on another day carries its date, so the times never seem to run backwards
  const at = (iso: string) => (fmtMD(iso) === fmtMD(ev.opened_at) ? fmtHM(iso) : `${fmtMD(iso)} ${fmtHM(iso)}`)
  const steps = [`${fmtMD(ev.opened_at)} ${fmtHM(ev.opened_at)} 發生`]
  if (ev.repaired_at) steps.push(`${at(ev.repaired_at)} 維修完成`)
  if (ev.recovered_at) steps.push(`${at(ev.recovered_at)} 恢復正常`)
  const took = closed ? `・共 ${dur(t(ev.closed_at) - t(ev.opened_at))}` : ''
  const result = closed ? outcomeText(ev.machine_id, ev.state, ev.outcome) : openPhase(ev, tasks)
  const head: Row[] = [{
    k: '經過', text: `${steps.join(' → ')}${took}｜${result}`,
    tone: ev.state === 'safe_stop' ? 'bad' : closed && !/未用讀值確認/.test(result) ? 'ok' : 'warn',
  }]

  const r = ev.ai_report
  const ai = r ? { tag: r.by === 'model' ? 'AI 撰寫' : '規則備援', lines: r.text.split('\n').filter((l) => l.trim()).slice(1).map(plain) } : null

  const parts: Part[] = []
  parts.push({
    id: 'timeline', title: '一、事件經過',
    rows: [...ev.timeline].sort((a, b) => t(a.ts) - t(b.ts)).map((x) => ({ time: fmtHM(x.ts), k: ACTOR[x.actor] ?? x.actor, text: tidy(x.text), tone: x.tag === 'safety' ? 'bad' : x.tag === 'human' ? 'ok' : undefined })),
    empty: '沒有時間軸紀錄',
  })

  parts.push({
    id: 'cause', title: '二、原因判斷',
    rows: ev.hypotheses.map((h) => {
      const e = h.expectation
      const exp = e ? `；預期 ${machineLabel(e.machine_id)} ${CHANNEL[e.channel] ?? e.channel} ${e.op === 'between' ? `${e.value}–${e.value_hi}` : `${e.op} ${e.value}`}${UNIT[e.channel] ?? ''}（${e.within_min} 分鐘內）` : ''
      const st = hypLabel(h.status, ev)
      return { k: `${hypName(h.id)}・${st}`, text: `${tidy(h.statement)}${exp}${h.evidence.length ? `；依據：${h.evidence.map(tidy).join('、')}` : ''}`, tone: h.status === 'rejected' ? 'bad' : h.status === 'confirmed' ? 'ok' : 'warn' }
    }),
    empty: '沒有提出判斷',
  })

  const runs: StepRun[] = [...(ev.prior_rounds ?? []).flatMap((r) => r.steps), ...ev.steps]
  const done = runs.filter((s) => s.state === 'ok' || s.state === 'failed' || s.state === 'rejected')
  parts.push({
    id: 'actions', title: '三、處置',
    rows: [
      ...ev.safety_actions.map((a) => ({ time: fmtHM(a.ts), k: '安全核心', text: `${tidy(a.message)}（${a.latency_ms.toFixed(0)} ms，不經 AI）`, tone: 'bad' as const })),
      ...done.map((s) => {
        const data = s.result?.data ?? {}
        // 3 at once: a schedule confirmed after another event's took effect is stale and gets re-solved (an English ToolError)
        const err = /is stale/.test(s.result?.error ?? '') ? '排程已先被另一件異常更新，改依現行排程重算' : s.result?.error ?? ''
        const txt = s.result?.ok ? resultText(s.step.tool, s.step.args, data) : s.state === 'rejected' ? '主管未核准，未執行' : `失敗：${err}`
        return { k: TOOL_LABEL[s.step.tool] ?? s.step.tool, text: `${tidy(s.step.purpose)}${txt ? `：${tidy(txt)}` : ''}`, tone: s.state === 'ok' ? 'ok' as const : 'warn' as const }
      }),
    ],
    empty: '沒有執行任何步驟',
  })

  const mine = approvals.filter((a) => a.event_id === ev.id).sort((a, b) => t(a.created_at) - t(b.created_at))
  const redone = staleCards(mine)
  const round = (n: number) => (n > 1 ? `第 ${n} 輪` : '')   // 第 N 輪 from the second round on
  parts.push({
    id: 'decisions', title: '四、主管決定',
    rows: mine.filter((a) => a.status !== 'superseded').map((a) => {
      const d = a.decision
      const opt = a.options.find((o) => o.id === d?.option_id)
      const by = [d?.decided_by ? who(d.decided_by) : null, d?.via ? VIA[d.via] ?? d.via : null].filter(Boolean).join('・')
      // the schedule a stale card names never took effect: its option keeps 「第 2 版排程」 without 「生效」
      const label = opt && (redone.has(a.id) ? tidy(opt.label).replace(/(第 \d+ 版排程)生效/g, '$1') : tidy(opt.label))
      return {
        time: fmtHM(d?.decided_at ?? a.created_at), k: [round(a.round), DECISION[a.status] ?? a.status].filter(Boolean).join('・'),
        text: `${tidy(a.title)}${opt ? `；選擇：${label}${opt.recommended ? '（系統建議）' : '（沒選系統建議）'}` : ''}${by ? `；${by}` : ''}${d?.reason ? `；理由：${tidy(d.reason)}` : ''}${redone.has(a.id) ? '；排程已先被更新，已重算' : ''}`,
        tone: (a.status === 'approved' || a.status === 'modified') && !redone.has(a.id) ? 'ok' : 'warn',
      }
    }),
    empty: '這件事件不需要主管核准',
  })

  parts.push({
    id: 'impact', title: '五、交期影響',
    rows: mine.filter((a) => (a.status === 'approved' || a.status === 'modified') && !redone.has(a.id))
      .map((a) => ({ time: fmtHM(a.decision?.decided_at ?? a.created_at), k: round(a.round) || undefined, text: tidy(a.delivery_impact) || '—' })),
    empty: '沒有核准過的排程變更',
  })

  const evTasks = tasks.filter((x) => x.event_id === ev.id)
  parts.push({
    id: 'tasks', title: '六、現場工作',
    rows: evTasks.map((x) => ({ time: fmtHM(x.done_at ?? x.created_at), k: `${x.kind}・${x.status}`, text: `${x.title}；${x.worker_name ?? '還沒派人'}`, tone: x.status === '完成' ? 'ok' : 'warn' })),
    empty: '沒有現場工作',
  })

  parts.push({
    id: 'audit', title: '八、稽核',
    rows: [
      { text: chainOk == null ? '讀取中' : chainOk ? '稽核紀錄完整，沒有被改過' : '稽核紀錄驗證失敗，請通知主管', tone: chainOk ? 'ok' : chainOk === false ? 'bad' : undefined },
      { text: `${auditCount != null ? `這件事共 ${auditCount} 筆紀錄・` : ''}事件編號 ${ev.id}` },
      // who wrote what: the Agent Nemo report is the rules' draft when it is tagged 規則備援
      { text: `${ai ? '最上面的異常報告由 Agent Nemo 撰寫（標「規則備援」時由規則整理）；' : ''}一～六節由系統紀錄整理，不經 AI；七由主管填寫。` },
    ],
  })
  return { title, head, ai, parts }
}

export function reportText(r: Report, prevention: string): string {
  const lines = [`【異常處理報告】${r.title}`, ...r.head.map((h) => `${h.k}：${h.text}`)]
  if (r.ai) lines.push('', `Agent Nemo 異常報告（${r.ai.tag}）`, ...r.ai.lines.map((l) => `・${l}`))
  for (const p of r.parts) {
    if (p.id === 'audit') lines.push('', '七、預防措施', prevention.trim() || '（未填）')
    lines.push('', p.title, ...(p.rows.length ? p.rows.map((x) => `・${x.time ? `${x.time} ` : ''}${x.k ? `${x.k}：` : ''}${x.text}`) : [`・${p.empty ?? '—'}`]))
  }
  return lines.join('\n').replace(/⁠/g, '')   // hypName's word joiners are for the screen, not for pasted text
}
