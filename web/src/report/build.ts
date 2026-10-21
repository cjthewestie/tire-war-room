// Issue #59: the incident report for one event, built by code from what the system recorded (the event's timeline,
// hypotheses, steps and safety actions, its approval cards, the worker tasks, the audit chain). No model writes it,
// so each line can be traced back to a record. The prevention measures are the one part a person writes.
import type { ApprovalRequest, Event, StepRun, WorkTask } from '../contracts'
import { STATE_LABEL } from '../components/common'
import { MACHINE_META, machineLabel } from '../factory'
import { fmtHM, fmtHMS, fmtMD } from '../format'
import { hypLabel, hypName, plain } from '../plain'
import { TOOL_LABEL, resultText } from '../stepText'

export interface ReportInput { ev: Event; approvals: ApprovalRequest[]; tasks: WorkTask[]; chainOk: boolean | null; auditCount: number | null }
export interface Row { k?: string; time?: string; text: string; tone?: 'ok' | 'warn' | 'bad' }
export interface Part { id: string; title: string; rows: Row[]; empty?: string }
export interface Report { title: string; head: Row[]; parts: Part[] }

const CHANNEL: Record<string, string> = { temp_c: '溫度', pressure_kgf_cm2: '壓力', water_flow_lpm: '水流', steam_kgf_cm2: '蒸汽壓' }
const UNIT: Record<string, string> = { temp_c: '°C', pressure_kgf_cm2: ' kgf/cm²', water_flow_lpm: ' L/min', steam_kgf_cm2: ' kgf/cm²' }
const DECISION: Record<string, string> = { approved: '核准', modified: '修改後核准', rejected: '否決', expired: '逾時未決', superseded: '被新版取代', pending: '待核准' }
const VIA: Record<string, string> = { phone: '手機', touchscreen: '現場螢幕', dashboard: '戰情室', auto: '自動' }

const t = (iso: string | null | undefined) => (iso ? Date.parse(iso) : NaN)
function dur(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '—'
  const m = Math.round(ms / 60000)
  return m < 60 ? `${m} 分鐘` : `${Math.floor(m / 60)} 小時 ${m % 60} 分`
}
const when = (iso: string | null | undefined) => (iso ? `${fmtMD(iso)} ${fmtHM(iso)}` : '—')
const whenS = (iso: string | null | undefined) => (iso ? `${fmtMD(iso)} ${fmtHMS(iso)}` : '—')

export function buildReport({ ev, approvals, tasks, chainOk, auditCount }: ReportInput): Report {
  const closed = ev.state === 'closed' || ev.state === 'safe_stop'
  // machine events: the machine and what went wrong (the alert card's title changes as the event moves on)
  const title = ev.machine_id in MACHINE_META ? `${machineLabel(ev.machine_id)}：${plain(ev.anomaly.message)}`
    : ev.alert_card?.title || ev.command || ev.anomaly.message || ev.id
  const head: Row[] = [
    { k: '事件編號', text: ev.id },
    { k: '機台', text: machineLabel(ev.machine_id) },
    { k: '異常', text: plain(ev.anomaly.message) },
    { k: '發生', text: when(ev.opened_at) },
    { k: '結案', text: closed ? when(ev.closed_at) : `尚未結案（${STATE_LABEL[ev.state]}）`, tone: closed ? 'ok' : 'warn' },
    { k: '處理時長', text: closed ? dur(t(ev.closed_at) - t(ev.opened_at)) : '—' },
    { k: '結果', text: plain(ev.outcome) || STATE_LABEL[ev.state], tone: ev.state === 'safe_stop' ? 'bad' : closed ? 'ok' : 'warn' },
  ]
  // fast repair (Kyle 10-21 flow): when the crew's repair cleared the fault, when the readings were back to normal, and
  // when the supervisor read this report (Event.review; nothing waited for it). Seconds: the steps are seconds apart.
  if (ev.repaired_at) head.push({ k: '維修完成', text: whenS(ev.repaired_at), tone: 'ok' })
  if (ev.recovered_at) head.push({ k: '恢復正常', text: whenS(ev.recovered_at), tone: 'ok' })
  const rv = ev.review
  if (rv) {
    const by = rv.by && rv.by !== 'supervisor' ? `・${rv.by}` : ''
    head.push(rv.status === 'done' ? { k: '主管已讀', text: `${whenS(rv.done_at)}${by}`, tone: 'ok' } : { k: '主管已讀', text: `尚未讀取（報告 ${whenS(rv.requested_at)} 產生）` })
  }

  const parts: Part[] = []
  parts.push({
    id: 'timeline', title: '一、事件經過',
    rows: [...ev.timeline].sort((a, b) => t(a.ts) - t(b.ts)).map((x) => ({ time: fmtHM(x.ts), k: x.actor, text: plain(x.text), tone: x.tag === 'safety' ? 'bad' : x.tag === 'human' ? 'ok' : undefined })),
    empty: '沒有時間軸紀錄',
  })

  parts.push({
    id: 'cause', title: '二、原因判斷',
    rows: ev.hypotheses.map((h) => {
      const e = h.expectation
      const exp = e ? `；預期 ${machineLabel(e.machine_id)} ${CHANNEL[e.channel] ?? e.channel} ${e.op === 'between' ? `${e.value}–${e.value_hi}` : `${e.op} ${e.value}`}${UNIT[e.channel] ?? ''}（${e.within_min} 分鐘內）` : ''
      const st = hypLabel(h.status, ev)
      return { k: `${hypName(h.id)}・${st}`, text: `${plain(h.statement)}${exp}${h.evidence.length ? `；依據：${h.evidence.map(plain).join('、')}` : ''}`, tone: h.status === 'rejected' ? 'bad' : h.status === 'confirmed' ? 'ok' : 'warn' }
    }),
    empty: '沒有提出判斷',
  })

  const runs: StepRun[] = [...(ev.prior_rounds ?? []).flatMap((r) => r.steps), ...ev.steps]
  const done = runs.filter((s) => s.state === 'ok' || s.state === 'failed' || s.state === 'rejected')
  parts.push({
    id: 'actions', title: '三、處置',
    rows: [
      ...ev.safety_actions.map((a) => ({ time: fmtHM(a.ts), k: '安全核心', text: `${plain(a.message)}（${a.latency_ms.toFixed(0)} ms，不經 AI）`, tone: 'bad' as const })),
      ...done.map((s) => {
        const data = s.result?.data ?? {}
        const txt = s.result?.ok ? resultText(s.step.tool, s.step.args, data) : s.state === 'rejected' ? '主管未核准，未執行' : `失敗：${s.result?.error ?? ''}`
        return { k: TOOL_LABEL[s.step.tool] ?? s.step.tool, text: `${plain(s.step.purpose)}${txt ? `：${plain(txt)}` : ''}`, tone: s.state === 'ok' ? 'ok' as const : 'warn' as const }
      }),
    ],
    empty: '沒有執行任何步驟',
  })

  const mine = approvals.filter((a) => a.event_id === ev.id).sort((a, b) => t(a.created_at) - t(b.created_at))
  parts.push({
    id: 'decisions', title: '四、主管決定',
    rows: mine.filter((a) => a.status !== 'superseded').map((a) => {
      const d = a.decision
      const opt = a.options.find((o) => o.id === d?.option_id)
      const who = [d?.decided_by, d?.via ? VIA[d.via] ?? d.via : null].filter(Boolean).join('・')
      return {
        time: fmtHM(d?.decided_at ?? a.created_at), k: `第 ${a.round} 輪・${DECISION[a.status] ?? a.status}`,
        text: `${plain(a.title)}${opt ? `；選擇：${opt.label}${opt.recommended ? '（系統建議）' : '（沒選系統建議）'}` : ''}${who ? `；${who}` : ''}${d?.reason ? `；理由：${d.reason}` : ''}`,
        tone: a.status === 'approved' || a.status === 'modified' ? 'ok' : 'warn',
      }
    }),
    empty: '這件事件不需要主管核准',
  })

  parts.push({
    id: 'impact', title: '五、交期影響',
    rows: mine.filter((a) => a.status === 'approved' || a.status === 'modified').map((a) => ({ k: `第 ${a.round} 輪`, text: plain(a.delivery_impact) || '—' })),
    empty: '沒有核准過的排程變更',
  })

  const evTasks = tasks.filter((x) => x.event_id === ev.id)
  parts.push({
    id: 'tasks', title: '六、現場任務',
    rows: evTasks.map((x) => ({ time: fmtHM(x.done_at ?? x.created_at), k: `${x.kind}・${x.status}`, text: `${x.title}；${x.worker_name ?? '還沒派人'}`, tone: x.status === '完成' ? 'ok' : 'warn' })),
    empty: '沒有派工',
  })

  parts.push({
    id: 'audit', title: '八、稽核',
    rows: [
      { text: chainOk == null ? '讀取中' : chainOk ? '稽核紀錄雜湊鏈完整，紀錄沒有被改過' : '稽核紀錄雜湊鏈驗證失敗，請通知主管', tone: chainOk ? 'ok' : chainOk === false ? 'bad' : undefined },
      ...(auditCount != null ? [{ text: `這件事件共有 ${auditCount} 筆稽核紀錄（狀態轉換、AI 呼叫、工具、把關、核准、安全動作）` }] : []),
      { text: '本報告由系統紀錄整理，不經 AI 撰寫；「七、預防措施」由主管填寫。' },
    ],
  })
  return { title, head, parts }
}

export function reportText(r: Report, prevention: string): string {
  const lines = [`【異常處理報告】${r.title}`, ...r.head.map((h) => `${h.k}：${h.text}`)]
  for (const p of r.parts) {
    if (p.id === 'audit') lines.push('', '七、預防措施', prevention.trim() || '（未填）')
    lines.push('', p.title, ...(p.rows.length ? p.rows.map((x) => `・${x.time ? `${x.time} ` : ''}${x.k ? `${x.k}：` : ''}${x.text}`) : [`・${p.empty ?? '—'}`]))
  }
  return lines.join('\n').replace(/\u2060/g, '')   // hypName's word joiners are for the screen, not for pasted text
}
