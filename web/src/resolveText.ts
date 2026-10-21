// Text for the resolution paths (components/Resolve.tsx, warroom/api/resolve.py), shared by the 主控台, its list and the phone.
import type { SafetyCase, SafetyDecision } from './contracts'
import { machineLabel } from './factory'

export const VERB: Record<string, string> = { stop_machine: '停機', hold_batch: '扣批', cure_hold: '扣模', alarm_only: '警示' }
export const DECISION: Record<SafetyDecision, string> = { resume: '確認恢復運轉', qc_pass: 'QC 合格，放行', scrap: '報廢' }

/** 「0.2 秒內」 under 200 ms (the page-9 target), else the seconds it took (a confirm costs one sample period). */
export function latencyText(ms: number): string {
  return ms < 200 ? '0.2 秒內' : `${(ms / 1000).toFixed(1)} 秒`
}

/** 「⛔ 安全核心：B 線開煉機 120°C 停機（0.2 秒內，未經 AI）」 */
export function caseTitle(c: SafetyCase): string {
  const lim = c.rule_id === 'SR-DATA-LOSS' ? '訊號中斷' : `${c.threshold.toFixed(0)}°C`
  return `⛔ 安全核心：${machineLabel(c.machine_id)} ${lim} ${VERB[c.action] ?? '處置'}（${latencyText(c.latency_ms)}，未經 AI）`
}

export function openCases(cases: SafetyCase[] | undefined): SafetyCase[] {
  return (cases ?? []).filter((c) => c.status === 'open')
}
