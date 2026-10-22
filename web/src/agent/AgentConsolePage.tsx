// Agent page (#console, issue #37): the full transcript of one event — every state change, the plan and
// hypotheses the commander returned, each tool call with its args, result and duration, the data it read
// (sensor readings, orders, SOP hits with their source), gate verdicts, approvals and replans — with filters
// and an event picker. CJ, 2026-10-20: 「在那個大的單獨的 agent 的 page 那邊才要去有這些步驟讓我們看到她思考的過程
// 跟調用工具調用資料的過程」. Rows come from the same audit-log transcript as the home panel (useTranscript).
// Fast repair (Kyle 10-21): the 處理歷程 also has the workers' tasks, 維修完成, 已恢復正常, 異常報告已產生 and 主管已讀報告, and
// ends with the 處理報告 once the event closes (ReportBlock).
import { useMemo, useState } from 'react'
import type { Event, EventState, TaskRow, ToolName } from '../contracts'
import { machineLabel } from '../factory'
import { fmtHMS, fmtMD, fmtMs } from '../format'
import { plain } from '../plain'
import { TOOL_LABEL } from '../stepText'
import { STATE_LABEL } from '../components/common'
import { StateDiagram } from './StateDiagram'
import { pathOf, stateAt } from './stateGraph'
import { useWarRoom } from '../useWarRoom'
import { ToolIcon } from './ToolIcon'
import { withAsks, type Item, type LocalItem, type ToolItem } from './transcript'
import { Row } from './TranscriptRows'
import { ChatBar, type BarFocus } from './ChatBar'
import { SUB_STATUS, SUB_TONE, eventOpen, plainStatus, whatOf } from './subagents'
import { BubbleRow, Bubbles } from './SimpleChat'
import { useMainThread } from './mainThread'
import { TaskList } from '../components/TaskList'
import { useTranscript } from './useTranscript'
import { ReportBlock } from './ReportBlock'
import { TodaySummary } from './TodaySummary'
import { TaskSessions } from './TaskSessions'
import { allRows, useStoredSession, useTaskListing } from './sessionStore'
import './agent.css'

type Kind = 'all' | 'state' | 'think' | 'tool' | 'data' | 'gate' | 'approval' | 'talk'
const KINDS: { id: Kind; label: string; hint: string }[] = [
  { id: 'all', label: '全部', hint: '每一步' },
  { id: 'state', label: '狀態', hint: '狀態變化、事件偵測、安全核心、人員任務、維修與恢復' },
  { id: 'think', label: '計畫與判斷', hint: 'Agent Nemo 回傳的計畫、假設、程式驗證' },
  { id: 'tool', label: '工具', hint: '每次工具呼叫：參數、結果、耗時' },
  { id: 'data', label: '讀資料', hint: '只讀的工具：感測器、預測、SOP、訂單班表、排程求解' },
  { id: 'gate', label: '把關', hint: '動作把關的判定與攔截' },
  { id: 'approval', label: '核准', hint: '送核准、主管決定、核准卡改版、主管條件、異常報告' },
  { id: 'talk', label: '對話', hint: '主管訊息與 Agent 回覆' },
]
const READ_TOOLS: ToolName[] = ['read_sensors', 'predict_anomaly', 'cure_formula', 'sop_search', 'query_orders_shifts', 'solve_schedule']

function inKind(i: Item, k: Kind): boolean {
  switch (k) {
    case 'all': return true
    case 'state': return i.k === 'state' || i.k === 'anomaly' || i.k === 'safety' || i.k === 'task' || i.k === 'review'
    case 'think': return i.k === 'think' || i.k === 'note'
    case 'tool': return i.k === 'tool'
    case 'data': return i.k === 'tool' && READ_TOOLS.includes(i.tool)
    case 'gate': return i.k === 'gate' || i.k === 'blocked'
    case 'approval': return i.k === 'approval' || i.k === 'version' || i.k === 'chips' || i.k === 'review'
    case 'talk': return i.k === 'chat' || i.k === 'ask'
  }
}


const MAIN = '__main'
/** 總覽 questions (CJ 2026-10-22: 統合整理進行中的任務、查詢過去資料、列出有疑問的任務) */
const OVERVIEW_QUESTIONS = ['現在所有任務狀況', '哪些任務有問題', '今天處理過什麼', '#A-311 之前怎麼了']

export function AgentConsolePage({ asks, setAsks, initial, onSession }: {
  asks: LocalItem[]; setAsks: React.Dispatch<React.SetStateAction<LocalItem[]>>; initial: string | null
  /** a task link in the 總覽 conversation: open that task's session on the 主控台 (issue #189) */
  onSession?: (eventId: string) => void
}) {
  const { state } = useWarRoom()
  const events = useMemo(() => [...(state.snapshot?.events ?? [])].sort((a, b) => (a.opened_at < b.opened_at ? 1 : -1)), [state.snapshot?.events])
  const [picked, setPicked] = useState<string | null>(initial ?? MAIN)
  const [seen, setSeen] = useState(initial)
  if (initial !== seen) { setSeen(initial); if (initial) setPicked(initial) }
  const main = picked === MAIN
  const ev = main ? null : events.find((e) => e.id === picked) ?? null
  const pick = (id: string) => {
    setPicked(id)
    history.replaceState(null, '', `${location.pathname}${location.search}#console${id === MAIN ? '' : `?event=${encodeURIComponent(id)}`}`)
  }
  const subs = new Map((state.subagents?.items ?? []).map((x) => [x.id, x]))
  // open / ended by the event's state and the World's worker tasks, as 主控台 lists it (not the subagent status: a fast
  // repair's closed event stayed under 處理中 as 「等現場完成（1/5）」 while 主控台 said 已結案, #92 review)
  const tasksOf = (e: Event) => (state.world?.tasks?.items ?? []).filter((t) => t.event_id === e.id)
  const openOf = (e: Event) => eventOpen(e, tasksOf(e))
  const listed = events.filter(openOf)          // the narrow-screen picker: the tasks at work (the list below has the rest)
  // 總覽: Agent Nemo over every task — its own conversation (mode overview), no task selected (CJ 2026-10-22)
  const thread = useMainThread(asks, 'overview')
  const [focus, setFocus] = useState<BarFocus | null>(null)
  if (!state.snapshot) return <div className="loading">等待工廠資料…</div>
  return (
    <>
    <ModelQueueBar />
    <div className="trace" data-testid="agent-page">
      <aside className="trace-side panel" aria-label="總覽與處理中的事件">
        <button type="button" className={`tree-root ${main ? 'on' : ''}`} onClick={() => pick(MAIN)} data-testid="tree-root" aria-pressed={main}>
          <span className="tree-title"><span className="tree-dot" aria-hidden />總覽</span>
          <span className="muted small">事件 {events.length}・進行中 {events.filter(openOf).length}</span>
        </button>
        <select className="trace-pick" value={main ? MAIN : picked ?? MAIN} onChange={(e) => pick(e.target.value)} aria-label="Agent Nemo 或事件">
          <option value={MAIN}>總覽</option>
          {!main && picked && !listed.some((e) => e.id === picked) && <option value={picked}>{ev ? evName(ev) : picked}</option>}
          {listed.map((e) => { const sa = subs.get(e.id); return <option key={e.id} value={e.id}>{sa ? `${whatOf(sa, e)}：${plainStatus(sa, e, tasksOf(e))}` : evName(e)}</option> })}
        </select>
        {/* issue #189: 進行中 / 今日已結案 / 歷史任務 (by day), searchable; each row is that task's session */}
        <div className="trace-tasks sc">
          <TaskSessions current={main ? null : picked} onPick={(r: TaskRow) => pick(r.id)} testId="trace-sessions" status={(r) => {
            const e = r.live ? events.find((x) => x.id === r.id) : undefined
            const sa = e ? subs.get(e.id) : undefined
            return e && sa && r.running ? (
              <span className="tree-row" data-testid="sub-status">{plainStatus(sa, e, tasksOf(e))}
                {sa.tasks_total > 0 && <span className="sub-bar" aria-hidden><i style={{ width: `${(100 * sa.tasks_done) / sa.tasks_total}%` }} /></span>}
              </span>
            ) : null
          }} />
        </div>
      </aside>
      {main ? (
        <section className="panel trace-main main-thread" aria-label="總覽" data-testid="main-thread" data-mode="overview">
          <h2 className="trace-h">總覽</h2>
          <TodaySummary />
          <Bubbles items={thread} empty="問全部任務的狀況、哪些有問題、過去的紀錄，或工廠的任何事。" onLink={onSession ?? pick} guide={(
            <div className="con-guide" data-testid="overview-guide">
              <p className="muted small">我看得到所有進行中、今日已結案和歷史任務，以及工廠資料。提到的任務會附連結，點了到主控台打開那件任務的對話。</p>
              {OVERVIEW_QUESTIONS.map((q) => <button key={q} type="button" className="con-guide-q" onClick={() => setFocus((f) => ({ text: q, send: true, n: (f?.n ?? 0) + 1 }))}>{q}</button>)}
            </div>
          )} />
          <div className="trace-chat" data-testid="trace-chat"><ChatBar ev={null} waiting={false} setAsks={setAsks} focus={focus} mode="overview" /></div>
        </section>
      ) : ev ? <Trace ev={ev} asks={asks} setAsks={setAsks} /> : <PastTask id={picked ?? ''} onSession={onSession} />}
    </div>
    </>
  )
}

/** A task of an earlier day (or before a restart / scenario load): its row and its session from the chat log, read-only. */
function PastTask({ id, onSession }: { id: string; onSession?: (eventId: string) => void }) {
  const listing = useTaskListing('')
  const row = allRows(listing).find((r) => r.id === id)
  const { bubbles, loaded } = useStoredSession(id)
  return (
    <section className="panel trace-main" aria-label="歷史任務" data-testid="past-task">
      <header className="trace-h">
        <div>
          <h2>{row ? plain(row.title) : id}</h2>
          <p className="muted small trace-id">{id}{row?.day ? `・${row.day}` : ''}{row ? `・${row.kind_label}` : ''}</p>
        </div>
        {row && <span className="trace-h-r"><span className="state-pill">{row.state_label}</span></span>}
      </header>
      {!row && listing && <p className="muted">找不到這個任務。</p>}
      {row?.outcome && <p className="small">{plain(row.outcome)}</p>}
      <p className="muted small">這個任務已不在目前的情境裡：處理過程請看處理報告，下面是它的 Agent Nemo 對話（只能查看）。</p>
      <p className="small"><a href={`/report?event=${encodeURIComponent(id)}`} target="_blank" rel="noreferrer">處理報告 →</a>
        {onSession && <>　<a href="#" onClick={(e) => { e.preventDefault(); onSession(id) }} data-testid="past-open-console">在主控台打開這個對話 →</a></>}</p>
      <div className="con-body trace-body" data-testid="past-session">
        {!bubbles.length && <p className="muted con-empty">{loaded ? '這個任務沒有對話紀錄。' : '讀取對話紀錄…'}</p>}
        {bubbles.map((b) => <BubbleRow key={b.key} b={b} />)}
      </div>
    </section>
  )
}

/** Who waits for Nemotron now (the main agent's priority queue: safety > machine > orders/materials > staffing > questions). */
function ModelQueueBar() {
  const q = useWarRoom().state.subagents?.queue
  if (!q) return null
  const label = (t: { label: string; who: string; event_id: string | null }) => t.label || t.who
  return (
    <section className="mq panel" aria-label="模型排隊" data-testid="model-queue">
      <b>模型排隊</b>
      <span className="muted small">一次 {q.slots} 個・優先：安全 ＞ 機台故障 ＞ 訂單／原料 ＞ 人力 ＞ 提問</span>
      <span className="mq-items">
        {q.running.map((t, i) => <span key={`r${i}`} className="mq-t run" title={t.event_id ?? ''}><i className="spin" />{label(t)}<small>{t.priority_label}</small></span>)}
        {q.waiting.map((t, i) => <span key={`w${i}`} className="mq-t" title={t.event_id ?? ''}>{i + 1}. {label(t)}<small>{t.priority_label}</small></span>)}
        {!q.running.length && !q.waiting.length && <span className="muted small">目前沒有人在等模型</span>}
      </span>
    </section>
  )
}

/** The selected event: where it stands, its workers' tasks, and 「中斷」 (no dispatch or typed agent: CJ 2026-10-21). */
function EventHead({ ev }: { ev: Event }) {
  const { state, transport } = useWarRoom()
  const sa = state.subagents?.items.find((x) => x.id === ev.id)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const tasks = (state.world?.tasks?.items ?? []).filter((t) => t.event_id === ev.id)
  const open = ev.state !== 'closed' && ev.state !== 'safe_stop'
  const stop = async () => {
    if (!transport?.interrupt) return
    if (!window.confirm(`中斷這件事（${ev.id}）？\n不會再呼叫工具；待核准的卡作廢、還沒做完的人員任務取消；排程維持現狀。這個動作會寫進稽核紀錄。`)) return
    setBusy(true); setMsg(null)
    try { await transport.interrupt(ev.id); setMsg('已中斷') } catch (e) { setMsg(`中斷失敗：${String(e).replace(/^Error: /, '')}`) } finally { setBusy(false) }
  }
  return (
    <section className="sub-head" aria-label="事件狀態" data-testid="sub-head">
      <div className="sub-head-row">
        <b>{evName(ev)}</b>
        {sa && <span className={`sub-pill t-${SUB_TONE[sa.status]}`}>{SUB_STATUS[sa.status]}</span>}
        {open && transport?.interrupt && <button className="btn-ghost danger sub-stop" onClick={() => void stop()} disabled={busy} data-testid="interrupt">{busy ? '中斷中…' : '中斷'}</button>}
      </div>
      {msg && <p className="muted small">{msg}</p>}
      {tasks.length > 0 && <div className="sub-tasks"><h4 className="sub">人員任務 {tasks.filter((t) => t.status === '完成').length}/{tasks.length}</h4><TaskList tasks={tasks} eventId={ev.id} testId="sub-tasks" compact /></div>}
    </section>
  )
}

function evName(e: Event): string {
  return e.source === 'supervisor' ? '主管指令' : e.source === 'plant' ? '工廠事件' : machineLabel(e.machine_id)
}

function Trace({ ev, asks, setAsks }: {
  ev: Event | null; asks: LocalItem[]; setAsks: React.Dispatch<React.SetStateAction<LocalItem[]>>
}) {
  const { state } = useWarRoom()
  const { items: evItems, source } = useTranscript(ev)
  // the one conversation store: what this page and 主控台 are sending or asking, merged by time with the audit rows
  const items = useMemo(() => (ev ? evItems : withAsks([], asks)), [ev, evItems, asks])   // a subagent shows its own run only
  const talk = useMainThread(asks, 'task', ev?.id ?? null)    // this task's session (issue #189)
  const waiting = !!ev && ev.state === 'await_approval' && state.pending.some((p) => p.event_id === ev.id)
  const ended = !!ev && (ev.state === 'closed' || ev.state === 'safe_stop')
  const [kind, setKind] = useState<Kind>('all')
  const [open, setOpen] = useState(false)
  const [pick, setPick] = useState<EventState | null>(null)
  const at = useMemo(() => stateAt(items), [items])
  const path = useMemo(() => pathOf(items), [items])
  const shown = items.filter((i, n) => inKind(i, kind) && (!pick || at[n] === pick))
  const tools = items.filter((i): i is ToolItem => i.k === 'tool')
  const toolMs = tools.reduce((a, t) => a + (t.durationMs ?? 0), 0)
  const thinkIdx = new Map<string, number>()
  let n = 0
  for (const i of items) if (i.k === 'think' && i.stepIds.length) thinkIdx.set(i.key, n++)
  return (
    <section className="panel trace-main" aria-label="Agent 完整過程">
      <header className="trace-h">
        <div>
          <h2>{ev ? <>{evName(ev)}・{fmtMD(ev.opened_at)} {fmtHMS(ev.opened_at)}</> : '問戰情室（只查不改）'}</h2>
          {ev && <p className="muted small trace-id">{ev.id}{ev.goal ? `・目標：${plain(ev.goal)}` : ''}</p>}
        </div>
        {ev && <span className="trace-h-r">
          {ended && <button type="button" className="btn-ghost" data-testid="trace-to-report"
            onClick={() => document.getElementById(`report-${ev.id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>看處理報告 ↓</button>}
          <span className={`state-pill st-${ev.state}`}>{STATE_LABEL[ev.state]}</span>
        </span>}
      </header>
      {ev && <EventHead ev={ev} />}
      {ev && <StateDiagram ev={ev} path={path} pick={pick} onPick={setPick} />}
      <dl className="trace-stats">
        <div><dt>步驟</dt><dd>{items.length}</dd></div>
        <div><dt>工具呼叫</dt><dd>{tools.length}<small>{tools.length ? `・共 ${fmtMs(toolMs)}` : ''}</small></dd></div>
        <div><dt>計畫</dt><dd>{items.filter((i) => i.k === 'think').length}<small>{ev?.replans ? `・重新規劃 ${ev.replans}` : ''}</small></dd></div>
        <div><dt>把關</dt><dd>{items.filter((i) => i.k === 'gate' || i.k === 'blocked').length}</dd></div>
        <div><dt>核准</dt><dd>{items.filter((i) => i.k === 'approval').length}</dd></div>
      </dl>
      {tools.length > 0 && <DataSummary tools={tools} />}
      <div className="trace-filters" role="group" aria-label="篩選">
        {KINDS.map((k) => {
          const c = items.filter((i) => inKind(i, k.id)).length
          return (
            <button key={k.id} className={kind === k.id ? 'on' : ''} aria-pressed={kind === k.id} title={k.hint} onClick={() => setKind(k.id)} disabled={!c && k.id !== 'all'} data-testid={`trace-filter-${k.id}`}>
              {k.label}<small>{c}</small>
            </button>
          )
        })}
        <label className="trace-open"><input type="checkbox" checked={open} onChange={(e) => setOpen(e.target.checked)} />展開工具細節</label>
      </div>
      <p className="muted small trace-note">Agent Nemo 以推理模式關閉執行：「計畫與判斷」是模型回傳的計畫與假設，不是隱藏的思考過程。每一列都來自稽核紀錄{source === 'event' ? '（瀏覽器模擬模式：由事件重建）' : ''}。</p>
      <div className="con-body trace-body" data-testid="trace-body">
        {pick && <p className="muted small">只顯示在「{STATE_LABEL[pick]}」狀態時寫下的 {shown.length} 筆</p>}
        {!shown.length && <p className="muted con-empty">{items.length ? '這個篩選沒有紀錄' : '讀取稽核紀錄中…'}</p>}
        {shown.map((it) => <Row key={it.key} it={it} ev={ev} nth={it.k === 'think' ? thinkIdx.get(it.key) ?? 0 : 0} open={open} />)}
        {ev && ended && <ReportBlock ev={ev} />}
        {talk.length > 0 && <div className="sc sp-talk" data-testid="trace-talk">{talk.map((b) => <BubbleRow key={b.key} b={b} />)}</div>}
      </div>
      <div className="trace-chat" data-testid="trace-chat"><ChatBar ev={ev} waiting={waiting} setAsks={setAsks} focus={null} /></div>
    </section>
  )
}

/** 讀了哪些資料: one line per read tool, SOP hits with their source. */
function DataSummary({ tools }: { tools: ToolItem[] }) {
  const reads = tools.filter((t) => READ_TOOLS.includes(t.tool))
  const by = new Map<ToolName, number>()
  for (const t of reads) by.set(t.tool, (by.get(t.tool) ?? 0) + 1)
  const sop = new Map<string, { title: string; source: string; score: number | null }>()
  for (const t of reads.filter((x) => x.tool === 'sop_search' && x.data)) {
    for (const h of [...arr(t.data!.hits), ...arr(t.data!.team_hits)]) {
      const id = String(h.sop_id ?? '')
      if (id && !sop.has(id)) sop.set(id, { title: String(h.title ?? ''), source: String(h.source ?? ''), score: typeof h.score === 'number' ? h.score : null })
    }
  }
  const presses = new Set<string>()
  for (const t of reads.filter((x) => x.tool === 'read_sensors' && x.data)) for (const id of Object.keys((t.data!.summary ?? {}) as object)) presses.add(id)
  return (
    <section className="trace-data" aria-label="讀了哪些資料">
      <h3 className="sub">讀了哪些資料</h3>
      <ul>
        {[...by.entries()].map(([tool, c]) => (
          <li key={tool}><ToolIcon name={tool} /><b>{TOOL_LABEL[tool]}</b> ×{c}
            {tool === 'read_sensors' && presses.size > 0 && <span className="muted">：{[...presses].map(machineLabel).join('、')}</span>}
          </li>
        ))}
      </ul>
      {sop.size > 0 && (
        <ul className="trace-sop">
          {[...sop.entries()].map(([id, h]) => (
            <li key={id}><b>{id}</b> {h.title}<small>{h.source === 'team_dataset' ? '團隊資料集' : h.source}{h.score != null ? `・${h.score.toFixed(3)}` : ''}</small></li>
          ))}
        </ul>
      )}
    </section>
  )
}

type D = Record<string, unknown>
const arr = (v: unknown): D[] => (Array.isArray(v) ? (v as D[]) : [])
