// The task list with search (issue #189; CJ, 2026-10-22: 「進行中任務也要可以搜尋的到 今日已結案也要 然後隔日已結案的任務會儲存到
// 歷史任務區分每天」): 進行中, 今日已結案 and 歷史任務 (one fold per factory day, newest first). Each row is a task's Agent
// Nemo session; picking one opens it. Used by the 主控台 session switcher and the Agent page's side list.
import { useState, type ReactNode } from 'react'
import type { TaskListing, TaskRow } from '../contracts'
import { fmtHM } from '../format'
import { plain } from '../plain'
import { useTaskListing } from './sessionStore'

export function TaskSessions({ current, onPick, onGeneral, generalLabel = '新對話', status, testId = 'task-sessions' }: {
  /** the open session: an event id, or null for the 「新對話」 / 總覽 session */
  current: string | null
  onPick: (row: TaskRow) => void
  /** shows the 「新對話」 (or 總覽) row on top */
  onGeneral?: () => void
  generalLabel?: string
  /** an extra line under a live row (the Agent page: what its subagent is doing) */
  status?: (row: TaskRow) => ReactNode
  testId?: string
}) {
  const [q, setQ] = useState('')
  const list = useTaskListing(q)
  const searching = !!q.trim()
  return (
    <div className="tss" data-testid={testId}>
      {onGeneral && (
        <button type="button" className={`tss-row tss-general ${current === null ? 'on' : ''}`} aria-pressed={current === null} onClick={onGeneral} data-testid="session-general">
          <b>{list?.general_session?.session_short && <span className="tss-no" title={list.general_session.session ?? ''} data-testid="session-no">{list.general_session.session_short}</span>}{generalLabel}</b><span className="muted small">不綁任務的問題</span>
        </button>
      )}
      <label className="tss-search">
        <span className="sr-only">搜尋任務</span>
        <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜尋任務：機台、訂單、事件編號、類型…" maxLength={80} data-testid="task-search" />
      </label>
      {!list && <p className="muted small">讀取任務清單…</p>}
      {list && <Groups list={list} current={current} onPick={onPick} searching={searching} status={status} />}
    </div>
  )
}

function Groups({ list, current, onPick, searching, status }: {
  list: TaskListing; current: string | null; onPick: (row: TaskRow) => void; searching: boolean; status?: (row: TaskRow) => ReactNode
}) {
  const none = !list.running.length && !list.closed_today.length && !list.history.length
  return (
    <div className="tss-groups">
      <Group title="進行中" rows={list.running} current={current} onPick={onPick} status={status} empty={searching ? '沒有符合的進行中任務' : '目前沒有進行中的任務'} testId="tss-running" />
      <Group title={`今日已結案・${list.today.label}`} rows={list.closed_today} current={current} onPick={onPick} status={status} empty={searching ? '沒有符合的今日已結案任務' : '今天還沒有結案的任務'} testId="tss-today" />
      <section className="tss-g" data-testid="tss-history">
        <h4 className="tss-h">歷史任務<small className="muted">{list.history.reduce((a, g) => a + g.count, 0)}</small></h4>
        {!list.history.length && <p className="muted small tss-empty">{searching ? '沒有符合的歷史任務' : '還沒有前幾天的任務'}</p>}
        {list.history.map((g, i) => (
          <details key={g.key} className="tss-day" open={searching || i === 0 || g.tasks.some((t) => t.id === current)} data-testid="tss-day" data-day={g.key}>
            <summary>{g.label}<small className="muted">　{g.count} 件</small></summary>
            <Rows rows={g.tasks} current={current} onPick={onPick} />
          </details>
        ))}
      </section>
      {searching && none && <p className="muted small" role="status">沒有符合「{list.q}」的任務。</p>}
    </div>
  )
}

function Group({ title, rows, current, onPick, status, empty, testId }: {
  title: string; rows: TaskRow[]; current: string | null; onPick: (row: TaskRow) => void; status?: (row: TaskRow) => ReactNode; empty: string; testId: string
}) {
  return (
    <section className="tss-g" data-testid={testId}>
      <h4 className="tss-h">{title}<small className="muted">{rows.length}</small></h4>
      {rows.length ? <Rows rows={rows} current={current} onPick={onPick} status={status} /> : <p className="muted small tss-empty">{empty}</p>}
    </section>
  )
}

function Rows({ rows, current, onPick, status }: { rows: TaskRow[]; current: string | null; onPick: (row: TaskRow) => void; status?: (row: TaskRow) => ReactNode }) {
  return (
    <ul className="tss-list">
      {rows.map((r) => (
        <li key={`${r.id}-${r.opened_at}`}>
          <button type="button" className={`tss-row ${r.id === current ? 'on' : ''}`} aria-current={r.id === current ? 'true' : undefined}
            onClick={() => onPick(r)} data-testid="task-row" data-event={r.id} data-live={r.live ? '1' : '0'} title={[r.session, r.id].filter(Boolean).join('・')}>
            {r.session_short && <span className="tss-no" data-testid="session-no">{r.session_short}</span>}
            <span className="tss-t">{r.opened_at ? fmtHM(r.opened_at) : ''}{r.closed_at ? `–${fmtHM(r.closed_at)}` : ''}</span>
            <b className="tss-title">{plain(r.title)}</b>
            <span className={`tss-st ${r.running ? 'run' : ''}`}>{r.state_label}</span>
            {status?.(r)}
            {!r.running && r.outcome && <small className="muted tss-out">{plain(r.outcome).slice(0, 60)}</small>}
          </button>
        </li>
      ))}
    </ul>
  )
}
