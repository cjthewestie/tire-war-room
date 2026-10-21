// 今天處理過的任務, read from GET /api/tasklog (the audit file, warroom/api/tasklog.py): shared by the Agent page
// (TaskLog.tsx) and 主控台's 今天處理過的事 (HomeOverview), so both say the same (veteran 10-22).
import { useCallback, useEffect, useState } from 'react'
import { apiGet } from '../transport'
import { useWarRoom } from '../useWarRoom'
import { plain } from '../plain'
import { SCENARIOS } from '../mock/engine'

export interface TaskRow {
  id: string; opened_at: string; closed_at: string | null; machine_id: string | null; order_id: string | null
  title: string | null; final: string; outcome: string | null; scenario: string | null; loop: number | null; report: string
  approvals: { decision: string; by: string; version: number }[]
}
export interface TaskDay { date: string; count: number; groups: { loop: number | null; scenario: string | null; rows: TaskRow[] }[] }

/** The durable task log, live only (null in the browser mock): every 8 s, and again as soon as an event closes (the
 *  conclusion beside it said 已恢復正常 while this still read 處理中 for up to 8 s). */
export function useTaskLog(): TaskDay[] | null {
  const { state, transport } = useWarRoom()
  const [days, setDays] = useState<TaskDay[]>([])
  const live = transport?.mode === 'live'
  const pull = useCallback((alive: () => boolean) => apiGet<{ days: TaskDay[] }>('/api/tasklog').then((d) => { if (alive()) setDays(d.days) }).catch(() => {}), [])
  useEffect(() => {
    if (!live) return
    let off = false
    void pull(() => !off)
    const t = setInterval(() => void pull(() => !off), 8000)
    return () => { off = true; clearInterval(t) }
  }, [live, pull])
  const ended = (state.snapshot?.events ?? []).filter((e) => e.state === 'closed' || e.state === 'safe_stop').map((e) => e.id).join(',')
  useEffect(() => {
    if (!live || !ended) return
    let off = false
    void pull(() => !off)
    return () => { off = true }
  }, [live, ended, pull])
  return live ? days : null
}

/** 「正常運轉」 for the scenario a run belongs to (not its code name). */
export const scenarioLabel = (name: string | null) => (name ? SCENARIOS.find((s) => s.name === name)?.label ?? name : '')
/** 「主管核准」: who decided (the API's default actor is supervisor), never the card version or the raw actor. */
export const decisionsText = (r: TaskRow) => [...new Set(r.approvals.map((a) => {
  const by = (a.by ?? '').replace(/@.*/, '')
  return `${/^(supervisor|dashboard|phone|touchscreen)?$/.test(by) ? '主管' : by === 'system' ? '系統' : ''}${a.decision}`
}))].join('、')
/** The outcome without the backend's 「已修復：」 head (veteran glossary 維修完成). */
export const outcomeText = (r: TaskRow) => plain(r.outcome).replace(/^已修復[：:]\s*/, '')
