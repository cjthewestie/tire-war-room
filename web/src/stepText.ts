// Judge-facing Chinese text for the commander step plan (⑥). Mirrors warroom/agent/narrate.py result_text.
import type { ToolName } from './contracts'
import { SIZE_LABEL, TEAM_LABELS, machineLabel } from './factory'

export const TOOL_LABEL: Record<ToolName, string> = {
  read_sensors: '讀取感測器', predict_anomaly: '異常預測', cure_formula: '硫化公式', solve_schedule: '排程求解',
  sop_search: 'SOP 知識庫', query_orders_shifts: '訂單／班表', confirm_schedule: '確認排程',
  set_cure_time: '設定硫化時間', notify_teams: '通知',
}

export const NOTIFY_LABEL: Record<string, string> = {
  maintenance: '通知維修', info: '通知', slow_feed: '放慢生料投入', pause_feed: '暫停投料',
  staff_move: '跨組調人', overtime: '加班', machine_out: '機台停用',
}

/** The staffing option the supervisor picked in a round: its approval-option step "A 夜班加班" → "夜班加班". */
export function staffingChoice(steps: { step: { id: string; purpose: string } }[]): string | undefined {
  // a '-revoke-' step is the withdrawal of an earlier pick, not the pick itself
  const ap = [...steps].reverse().find((s) => s.step.id.startsWith('AP-') && !isRevoke(s.step.id))
  return ap ? ap.step.purpose.replace(/^\p{Lu} /u, '') : undefined
}

/** The state machine's compensating notice for replaced staffing ("<approval id>-revoke-<n>"). */
export function isRevoke(id: string): boolean {
  return id.includes('-revoke-')
}

/** "S1" stays "S1"; an approval-option step "AP-EV-…-1-B" becomes "核准 B"; a withdrawal "↺ 撤回". */
export function stepLabel(id: string): string {
  if (isRevoke(id)) return '↺ 撤回'
  if (!id.startsWith('AP-')) return id
  const opt = id.split('-').pop()
  return opt && opt.length === 1 && opt >= "A" && opt <= "Z" ? `核准 ${opt}` : '核准'
}

type D = Record<string, unknown>
const num = (v: unknown, d = 1): string => {
  const n = typeof v === 'number' ? v : Number(v)
  if (v == null || isNaN(n)) return '—'
  return String(Math.round(n * 10 ** d) / 10 ** d)
}
const arr = (v: unknown): D[] => (Array.isArray(v) ? (v as D[]) : [])
const pressShort = (id: unknown): string => {
  const s = String(id ?? '')
  return s.startsWith('CURE-') ? `${Number(s.slice(5))} 號機` : machineLabel(s)
}
const teams = (v: unknown): string => (Array.isArray(v) ? v.map((t) => TEAM_LABELS[t as keyof typeof TEAM_LABELS] ?? String(t)).join('、') : '')

/** One short Chinese phrase for a tool result. Never dumps JSON. */
export function resultText(tool: ToolName, args: D, data: D, opts: { staffing?: string } = {}): string {
  switch (tool) {
    case 'cure_formula':
      return `${args.actual_temp_c != null ? `${num(args.actual_temp_c)}°C ` : ''}總硫化 ${num(data.total_cure_h)} h（+${num(data.extra_h)} h）`
    case 'query_orders_shifts': {
      const parts: string[] = []
      const job = arr(data.next_jobs)[0]
      if (job) parts.push(`下一模 ${job.order_id}${job.size ? `（${SIZE_LABEL[job.size as 'S' | 'M' | 'L'] ?? ''}）` : ''}`)
      parts.push(...arr(data.gaps).map(String))
      return parts.join('，') || '訂單班表無異動'
    }
    case 'read_sensors': {
      const summ = (data.summary ?? {}) as Record<string, D>
      return Object.entries(summ).map(([id, s]) => `${pressShort(id)} ${num(s.temp_c_now)}°C`).join('、') || '感測器讀值'
    }
    case 'predict_anomaly':
      return `預測 ${num(data.predicted_value)}°C（${num(data.band_lo)}–${num(data.band_hi)}）`
    case 'sop_search': {
      const h = arr(data.hits)[0]
      return h ? `SOP「${h.title}」` : 'SOP 無相符'
    }
    case 'solve_schedule': {
      const bits: string[] = []
      const grouped = new Map<string, number>()
      let retimed = 0
      for (const mv of arr(data.moves)) {
        if (mv.to_press && mv.from_press === mv.to_press) { retimed++; continue }
        const k = `${mv.order_id} → ${pressShort(mv.to_press)}`
        grouped.set(k, (grouped.get(k) ?? 0) + 1)
      }
      for (const [k, n] of grouped) bits.push(n > 1 ? `${k} ×${n}` : k)
      if (retimed) bits.push(`另 ${retimed} 模微調時間`)
      const sched = (data.schedule ?? {}) as D
      // The solver proposes staffing; the supervisor's pick (an approval-option step) is what ran.
      for (const c of arr(sched.staffing_changes)) {
        const l = String(c.label ?? '')
        bits.push(opts.staffing && l && l !== opts.staffing ? `人力：主管改選${opts.staffing}（求解器原提${l}）` : l)
      }
      if (data.deliveries_met) bits.push('交期全部滿足')
      else if (arr(data.late_orders).length) bits.push(`延誤 ${arr(data.late_orders).join('、')}`)
      bits.push(data.feasible === false ? '不可行' : `可行（${data.solver === 'heuristic' ? '備援求解器' : 'CP-SAT'}）`)
      if (data.fell_back) bits.push('求解逾時 → 備援')
      return bits.filter(Boolean).join('，')
    }
    case 'confirm_schedule': {
      const v = data.version ?? args.schedule_version
      const prev = data.previous_version
      return `排程 v${v} 生效${prev != null ? `（保留 v${prev}，可一鍵回復）` : ''}`
    }
    case 'set_cure_time':
      return `${pressShort(args.press_id)} 建議硫化 ${num(args.total_cure_h)} h`
    case 'notify_teams': {
      const t = teams(args.teams) || teams(data.teams) || teams(data.delivered)
      return `${NOTIFY_LABEL[String(args.kind)] ?? '通知'}${t ? ` → ${t}` : ''}`
    }
  }
  return TOOL_LABEL[tool] ?? tool
}
