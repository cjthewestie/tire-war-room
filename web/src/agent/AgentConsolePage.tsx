// Agent page (#console, issue #37): the full transcript of one event — every state change, the plan and
// hypotheses the commander returned, each tool call with its args, result and duration, the data it read
// (sensor readings, orders, SOP hits with their source), gate verdicts, approvals and replans — with filters
// and an event picker. CJ, 2026-10-20: 「在那個大的單獨的 agent 的 page 那邊才要去有這些步驟讓我們看到她思考的過程
// 跟調用工具調用資料的過程」. Rows come from the same audit-log transcript as the home panel (useTranscript).
// Fast repair (Kyle 10-21): the 處理歷程 also has the workers' tasks, 維修完成, 已恢復正常, 異常報告已產生 and 主管已讀報告.
// Veteran audit 10-22: each event opens on a conclusion (where it stands and the one thing to do), the Agent Nemo 異常報告
// right under it once the event ends (ReportBlock), then the 完整紀錄.
import { useMemo, useState } from 'react'
import type { ApprovalRequest, Event, EventState, ToolName, WorkTask } from '../contracts'
import { CH_LABEL, REPAIR_KINDS, expectationOf, recoverWord } from '../aiStatus'
import { machineLabel } from '../factory'
import { fmtHM, fmtMD, ms } from '../format'
import { useHealth } from '../health'
import { plain } from '../plain'
import { fastRepairOf, secText, taskLeftS, type FastRepair } from '../phone/fastEta'
import { TOOL_LABEL } from '../stepText'
import { STATE_LABEL } from '../components/common'
import { StateDiagram } from './StateDiagram'
import { pathOf, stateAt } from './stateGraph'
import { useWarRoom } from '../useWarRoom'
import { ToolIcon } from './ToolIcon'
import { withAsks, type ApprovalItem, type Item, type LocalItem, type ToolItem } from './transcript'
import { Row } from './TranscriptRows'
import { ChatBar } from './ChatBar'
import { anomalyWhat, backTo, endedText, isOpen, plainStatus, whatOf, workerName } from './subagents'
import { Bubbles } from './SimpleChat'
import { useMainThread } from './mainThread'
import { TaskList } from '../components/TaskList'
import { useTranscript } from './useTranscript'
import { ReportBlock } from './ReportBlock'
import './agent.css'

type Kind = 'all' | 'think' | 'data' | 'approval'
const KINDS: { id: Kind; label: string; hint: string }[] = [
  { id: 'all', label: '全部', hint: '每一步' },
  { id: 'think', label: '判斷', hint: '偵測、Agent Nemo 的計畫與判斷、程式驗證、安全把關、對話' },
  { id: 'data', label: '工具與資料', hint: '每次工具呼叫：參數、讀到的資料、結果、耗時' },
  { id: 'approval', label: '核准與現場工作', hint: '送核准、主管決定、核准卡重算、主管條件、現場工作、維修與恢復、異常報告' },
]
const READ_TOOLS: ToolName[] = ['read_sensors', 'predict_anomaly', 'cure_formula', 'sop_search', 'query_orders_shifts', 'solve_schedule']

function inKind(i: Item, k: Kind): boolean {
  switch (k) {
    case 'all': return true
    case 'think': return i.k === 'state' || i.k === 'anomaly' || i.k === 'safety' || i.k === 'think' || i.k === 'note' || i.k === 'gate'
      || i.k === 'blocked' || i.k === 'chat' || i.k === 'ask'
    case 'data': return i.k === 'tool'
    case 'approval': return i.k === 'approval' || i.k === 'version' || i.k === 'chips' || i.k === 'task' || i.k === 'review'
  }
}


const MAIN = '__main'

export function AgentConsolePage({ asks, setAsks, initial, onApprove }: {
  asks: LocalItem[]; setAsks: React.Dispatch<React.SetStateAction<LocalItem[]>>; initial: string | null
  /** 「到主控台核准 →」: the 主控台 with this event's card selected */
  onApprove?: (eventId: string) => void
}) {
  const { state } = useWarRoom()
  const events = useMemo(() => [...(state.snapshot?.events ?? [])].sort((a, b) => (a.opened_at < b.opened_at ? 1 : -1)), [state.snapshot?.events])
  const [picked, setPicked] = useState<string | null>(initial ?? MAIN)
  const [seen, setSeen] = useState(initial)
  if (initial !== seen) { setSeen(initial); if (initial) setPicked(initial) }
  // CJ, 2026-10-20: 處理中 first; closed and stopped ones sit under 已結束
  const [scope, setScope] = useState<'all' | 'open' | 'closed'>('open')
  const main = picked === MAIN
  const ev = main ? null : events.find((e) => e.id === picked) ?? null
  const pick = (id: string) => {
    setPicked(id)
    history.replaceState(null, '', `${location.pathname}${location.search}#console${id === MAIN ? '' : `?event=${encodeURIComponent(id)}`}`)
  }
  const subs = new Map((state.subagents?.items ?? []).map((x) => [x.id, x]))
  const tasks = state.world?.tasks?.items ?? []
  const openOf = (e: Event) => { const sa = subs.get(e.id); return sa ? isOpen(sa) : e.state !== 'closed' && e.state !== 'safe_stop' }
  const listed = events.filter((e) => scope === 'all' || (scope === 'open') === openOf(e))
  const thread = useMainThread(asks, true)
  if (!state.snapshot) return <div className="loading">等待工廠資料…</div>
  return (
    <>
    <ModelQueueBar />
    <div className="trace" data-testid="agent-page">
      <aside className="trace-side panel" aria-label="Agent Nemo 與處理中的事件">
        <button type="button" className={`tree-root ${main ? 'on' : ''}`} onClick={() => pick(MAIN)} data-testid="tree-root" aria-pressed={main}>
          <span className="tree-title"><span className="tree-dot" aria-hidden />Agent Nemo 對話</span>
        </button>
        <div className="trace-side-bar">
          <div className="seg sm" role="group" aria-label="事件範圍">
            {(['open', 'closed', 'all'] as const).map((s) => (
              <button key={s} className={scope === s ? 'on' : ''} aria-pressed={scope === s} onClick={() => setScope(s)}>{s === 'all' ? '全部' : s === 'open' ? '處理中' : '已結束'}</button>
            ))}
          </div>
          <select className="trace-pick" value={main ? MAIN : ev?.id ?? MAIN} onChange={(e) => pick(e.target.value)} aria-label="Agent Nemo 或事件">
            <option value={MAIN}>Agent Nemo 對話</option>
            {listed.map((e) => { const sa = subs.get(e.id); return <option key={e.id} value={e.id}>{sa ? `${whatOf(sa, e)}・${plainStatus(sa, e, tasks)}` : evName(e)}</option> })}
          </select>
        </div>
        <ul className="trace-list tree" role="listbox" aria-label="處理中的事件">
          {listed.map((e) => {
            const sa = subs.get(e.id)
            return (
              <li key={e.id} role="option" aria-selected={e.id === ev?.id}>
                <button className={e.id === ev?.id ? 'on' : ''} onClick={() => pick(e.id)} data-testid="trace-event" data-event={e.id} data-sub={sa?.status}>
                  <span className="tree-row" data-testid="sub-status"><b className="trace-what">{sa ? whatOf(sa, e) : evName(e)}</b>{sa && <small>{plainStatus(sa, e, tasks)}</small>}</span>
                  {sa && sa.tasks_total > 0 && <span className="sub-bar" aria-hidden><i style={{ width: `${(100 * sa.tasks_done) / sa.tasks_total}%` }} /></span>}
                </button>
              </li>
            )
          })}
          {!listed.length && <li className="muted small">{scope === 'open' ? '現在沒有處理中的事件。' : scope === 'closed' ? '還沒有已結束的事件。' : '還沒有事件。'}</li>}
        </ul>
      </aside>
      {main ? (
        <section className="panel trace-main main-thread" aria-label="Agent Nemo" data-testid="main-thread">
          <h2 className="trace-h">Agent Nemo</h2>
          <Bubbles items={thread} empty="跟 Agent Nemo 說要做的事，或問工廠現在的狀況。" />
          <div className="trace-chat" data-testid="trace-chat"><ChatBar ev={null} waiting={false} setAsks={setAsks} focus={null} /></div>
        </section>
      ) : ev ? <Trace ev={ev} asks={asks} setAsks={setAsks} onPick={pick} onApprove={onApprove} /> : (
        <section className="panel trace-main"><p className="muted">找不到這個事件。</p></section>
      )}
    </div>
    </>
  )
}

/** Who waits for Nemotron (the main agent's priority queue: safety > machine > orders/materials > staffing > questions);
 *  nothing while nobody waits. */
function ModelQueueBar() {
  const q = useWarRoom().state.subagents?.queue
  if (!q?.waiting.length) return null
  const label = (t: { label: string; who: string; event_id: string | null }) => t.label || t.who
  return (
    <section className="mq panel" aria-label="模型排隊" data-testid="model-queue">
      <b>Agent Nemo 同時處理 {q.running.length} 件，{q.waiting.length} 件排隊（安全優先）</b>
      <span className="mq-items">
        {q.waiting.map((t, i) => <span key={`w${i}`} className="mq-t" title={t.event_id ?? ''}>{i + 1}. {label(t)}<small>{t.priority_label}</small></span>)}
      </span>
    </section>
  )
}

type Tone = 'bad' | 'wait' | 'ok'
interface Answer { tone: Tone; big: string; more: string; act: 'approve' | 'report' | null }

/** 「11:42」, with the date when it is not the factory's today. */
const dueText = (iso: string, now: string | null) => `${now && fmtMD(iso) !== fmtMD(now) ? `${fmtMD(iso)} ` : ''}${fmtHM(iso)}`
const DOING: Record<string, string> = { 檢修: '維修' }

/** What Agent Nemo proposes on the waiting card, as it really is (veteran audit: the 10 號 card has no staffing option):
 *  「Agent Nemo 判斷蒸汽壓不足，建議派人檢查蒸汽供應與溫度計，這一模硫化延長到 41.1 小時，人力用方案 B（從 2 線調 1 人）。」 */
function proposal(ev: Event, card: ApprovalRequest, tasks: WorkTask[]): string {
  const hyp = (ev.plan?.hypothesis.statement ?? '').split('，')[0]
  const fix = tasks.find((t) => REPAIR_KINDS.has(t.kind))
  const ask = fix ? /請(.+)$/.exec(fix.title)?.[1] : null
  const cure = ev.steps.find((s) => s.step.tool === 'set_cure_time' && s.state === 'awaiting_approval')?.step.args.total_cure_h
  const opts = card.options
  const rec = opts.find((o) => o.recommended) ?? opts[0]
  const parts = [
    ask && `派人${ask}`,
    typeof cure === 'number' && `這一模硫化延長到 ${Math.round(cure * 10) / 10} 小時`,
    opts.length > 1 ? `人力用方案 ${rec.id}（${rec.label}）` : opts.length === 1 && opts[0].tool === 'notify_teams' ? opts[0].label : null,
  ].filter(Boolean)
  if (!hyp) return `Agent Nemo 擬好計畫：${plain(card.title)}${parts.length ? `，${parts.join('，')}` : ''}。`
  return `Agent Nemo 判斷${hyp}${parts.length ? `，建議${parts.join('，')}` : ''}。`
}

/** Where the event stands, in one sentence, and the one thing to do (veteran audit, Agent Nemo 頁 結論區): 請核准 by when,
 *  who is repairing it, the readings coming back, back to normal with the reading, or why it stopped. */
function conclusion(ev: Event, card: ApprovalRequest | null, tasks: WorkTask[], items: Item[], now: string | null, fast: FastRepair | null): Answer {
  const m = evName(ev)
  const subject = ev.source === 'supervisor' ? `主管指令「${ev.command ?? ''}」` : ev.source === 'plant' ? plain(ev.alert_card?.title ?? ev.anomaly.message) : `${m}${anomalyWhat(ev)}`
  const x = expectationOf(ev)
  const ch = x ? CH_LABEL[x.channel] : '讀值'
  if (ev.state === 'safe_stop') return { tone: 'bad', big: `${subject}：已停止`, more: plain(ev.outcome), act: null }
  if (ev.state === 'closed') {
    const end = endedText(ev)
    const open = tasks.filter((t) => t.status !== '完成').length
    if (end === '已恢復正常') {
      const val = backTo(ev.outcome)
      const at = ev.recovered_at ?? ev.closed_at
      const mins = at ? Math.max(1, Math.round((ms(at) - ms(ev.opened_at)) / 60_000)) : null
      const more = [mins && `從發生到恢復正常共 ${mins} 分鐘`, open > 0 && `還有 ${open} 件現場工作在做`, ev.ai_report && 'Agent Nemo 異常報告在下面']
      return { tone: 'ok', big: `${m}已恢復正常（${[val, at && fmtHM(at)].filter(Boolean).join('，')}）`, more: `${more.filter(Boolean).join('；')}。`, act: 'report' }
    }
    return { tone: end ? 'wait' : 'ok', big: end ? `${m}${end}` : `${subject}：已結束`, more: plain((ev.outcome ?? '').replace(/^已修復：/, '')), act: 'report' }
  }
  if (card) {
    return { tone: 'wait', big: `${subject}，請核准${card.expires_at ? `（${dueText(card.expires_at, now)} 前）` : ''}`, more: proposal(ev, card, tasks), act: 'approve' }
  }
  const decided = [...items].reverse().find((i): i is ApprovalItem => i.k === 'approval' && i.phase === 'decision' && (i.decision === 'approve' || i.decision === 'modify'))
  const opt = decided && decided.options.length > 1 ? decided.options.find((o) => o.id === decided.optionId) : null
  const approved = decided ? `主管 ${fmtHM(decided.ts)} 已核准${opt ? `方案 ${opt.id}「${opt.label}」` : ''}，` : ''
  if (ev.repaired_at) return { tone: 'wait', big: `${m}維修完成，${recoverWord(x)}`, more: `${fmtHM(ev.repaired_at)} 維修完成，${ch}回到正常才算完成。`, act: null }
  const fix = tasks.filter((t) => REPAIR_KINDS.has(t.kind) && t.status !== '完成')
  if (fix.length && (ev.state === 'execute' || ev.state === 'verify')) {
    const t = fix.find((f) => f.status !== '待派') ?? fix[0]
    const doing = DOING[t.kind] ?? t.kind
    const left = fast ? secText(Math.max(...fix.map((f) => taskLeftS(f, fast)))) : t.eta ? `預計 ${fmtHM(t.eta)} 完成` : ''
    const big = t.status === '待派' ? `Agent Nemo 正在派人${doing} ${m}` : `${workerName(t.worker_name)}${t.status === '前往中' ? '正前往' : '正在'}${doing} ${m}${left ? `（${left}）` : ''}`
    return { tone: 'bad', big, more: `${approved}${ch}回到正常才算完成。`, act: null }
  }
  if (ev.state === 'execute' || ev.state === 'verify') return { tone: 'wait', big: `${subject}，執行核准的計畫`, more: `${approved}程式會用讀值確認結果。`, act: null }
  const hyp = (ev.plan?.hypothesis.statement ?? '').split('，')[0]
  return { tone: 'bad', big: `${subject}，Agent Nemo 規劃中`, more: `${hyp ? `Agent Nemo 判斷${hyp}，` : ''}計畫擬好會送主管核准。`, act: null }
}

/** The selected event's conclusion: where it stands, its field work, 「到主控台核准 →」 or 「看異常報告 ↓」, and 「中斷處理」
 *  (no dispatch or typed agent: CJ 2026-10-21). */
function EventAnswer({ ev, items, onApprove }: { ev: Event; items: Item[]; onApprove?: (eventId: string) => void }) {
  const { state, transport } = useWarRoom()
  const fast = fastRepairOf(useHealth())
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  // the machine's own repair first: it is what turns the light green
  const tasks = (state.world?.tasks?.items ?? []).filter((t) => t.event_id === ev.id)
    .sort((a, b) => Number(REPAIR_KINDS.has(b.kind)) - Number(REPAIR_KINDS.has(a.kind)))
  const card = state.pending.find((p) => p.event_id === ev.id) ?? null
  const now = state.clock?.factory_time ?? state.snapshot?.header.factory_time ?? null
  const a = conclusion(ev, card, tasks, items, now, fast)
  const open = ev.state !== 'closed' && ev.state !== 'safe_stop'
  const stop = async () => {
    if (!transport?.interrupt) return
    if (!window.confirm(`要中斷${evName(ev)}這件事嗎？\nAgent Nemo 會停止處理，待核准的卡作廢，還沒做完的現場工作取消，排程維持現狀。這個動作會記入稽核紀錄。`)) return
    setBusy(true); setMsg(null)
    try { await transport.interrupt(ev.id); setMsg('已中斷') } catch (e) { setMsg(`中斷失敗：${String(e).replace(/^Error: /, '')}`) } finally { setBusy(false) }
  }
  return (
    <section className={`ev-answer t-${a.tone}`} aria-label="這件事現在怎樣" data-testid="sub-head" data-tone={a.tone}>
      <p className="ev-answer-at">{evName(ev)}・{fmtHM(ev.opened_at)} 發生</p>
      <h2>{a.big}</h2>
      <p className="ev-answer-more">{a.more}</p>
      <div className="ev-answer-act">
        {a.act === 'approve' && (onApprove
          ? <button type="button" className="btn-approve" onClick={() => onApprove(ev.id)} data-testid="answer-approve">到主控台核准 →</button>
          : <a className="btn-approve" href="#agent" data-testid="answer-approve">到主控台核准 →</a>)}
        {a.act === 'report' && ev.state === 'closed' && <button type="button" className="btn-ghost" data-testid="trace-to-report"
          onClick={() => document.getElementById(`report-${ev.id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>看異常報告 ↓</button>}
        {open && transport?.interrupt && <button className="btn-ghost danger sub-stop" onClick={() => void stop()} disabled={busy} data-testid="interrupt">{busy ? '中斷中…' : '中斷處理'}</button>}
      </div>
      {msg && <p className="muted small">{msg}</p>}
      {tasks.length > 0 && <div className="sub-tasks"><h4 className="sub">現場工作（完成 {tasks.filter((t) => t.status === '完成').length}/{tasks.length}）</h4><TaskList tasks={tasks} eventId={ev.id} testId="sub-tasks" compact /></div>}
    </section>
  )
}

function evName(e: Event): string {
  return e.source === 'supervisor' ? '主管指令' : e.source === 'plant' ? '工廠事件' : machineLabel(e.machine_id)
}

function Trace({ ev, asks, setAsks, onPick, onApprove }: {
  ev: Event | null; asks: LocalItem[]; setAsks: React.Dispatch<React.SetStateAction<LocalItem[]>>; onPick: (id: string) => void
  onApprove?: (eventId: string) => void
}) {
  const { state } = useWarRoom()
  const { items: evItems, source } = useTranscript(ev)
  // the one conversation store: what this page and 主控台 are sending or asking, merged by time with the audit rows
  const items = useMemo(() => (ev ? evItems : withAsks([], asks)), [ev, evItems, asks])   // a subagent shows its own run only
  const waiting = !!ev && ev.state === 'await_approval' && state.pending.some((p) => p.event_id === ev.id)
  const ended = !!ev && (ev.state === 'closed' || ev.state === 'safe_stop')
  const [kind, setKind] = useState<Kind>('all')
  const [open, setOpen] = useState(false)
  const [pick, setPick] = useState<EventState | null>(null)
  const at = useMemo(() => stateAt(items), [items])
  const path = useMemo(() => pathOf(items), [items])
  const shown = items.filter((i, n) => inKind(i, kind) && (!pick || at[n] === pick))
  const tools = items.filter((i): i is ToolItem => i.k === 'tool')
  const toolS = tools.reduce((a, t) => a + (t.durationMs ?? 0), 0) / 1000
  const thinkIdx = new Map<string, number>()
  let n = 0
  for (const i of items) if (i.k === 'think' && i.stepIds.length) thinkIdx.set(i.key, n++)
  return (
    <section className="panel trace-main" aria-label="Agent 完整過程">
      {ev ? <EventAnswer ev={ev} items={items} onApprove={onApprove} /> : <header className="trace-h"><h2>問戰情室（只查不改）</h2></header>}
      {ev && ended && <ReportBlock ev={ev} />}
      <section className="trace-full" aria-label="完整紀錄">
        <h3 className="sub trace-full-h">完整紀錄<small>（{items.length} 筆{tools.length ? `，工具 ${tools.length} 次，共 ${toolS.toFixed(1)} 秒` : ''}）</small></h3>
        {ev && <p className="muted small trace-id">事件編號 {ev.id}{ev.goal ? `・目標：${plain(ev.goal)}` : ''}</p>}
        {ev && <StateDiagram ev={ev} path={path} pick={pick} onPick={setPick} />}
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
          <label className="trace-open"><input type="checkbox" checked={open} onChange={(e) => setOpen(e.target.checked)} />顯示參數與原始資料</label>
        </div>
        <div className="con-body trace-body" data-testid="trace-body">
          {pick && <p className="muted small">只顯示在「{STATE_LABEL[pick]}」狀態時寫下的 {shown.length} 筆</p>}
          {!shown.length && <p className="muted con-empty">{items.length ? '這個篩選沒有紀錄' : '讀取稽核紀錄中…'}</p>}
          {shown.map((it) => <Row key={it.key} it={it} ev={ev} nth={it.k === 'think' ? thinkIdx.get(it.key) ?? 0 : 0} open={open} />)}
        </div>
        <p className="muted small trace-note">每一列都來自稽核紀錄{source === 'event' ? '（瀏覽器模擬模式：由事件重建）' : ''}。</p>
      </section>
      <div className="trace-chat" data-testid="trace-chat"><ChatBar ev={ev} waiting={waiting} setAsks={setAsks} focus={null} onSent={(about) => { if (!about) onPick(MAIN) }} /></div>
    </section>
  )
}

/** Agent Nemo 查了哪些資料: one line per read tool, the SOP it found. */
function DataSummary({ tools }: { tools: ToolItem[] }) {
  const reads = tools.filter((t) => READ_TOOLS.includes(t.tool))
  const by = new Map<ToolName, number>()
  for (const t of reads) by.set(t.tool, (by.get(t.tool) ?? 0) + 1)
  const sop = new Map<string, string>()
  for (const t of reads.filter((x) => x.tool === 'sop_search' && x.data)) {
    for (const h of [...arr(t.data!.hits), ...arr(t.data!.team_hits)]) {
      const id = String(h.sop_id ?? '')
      if (id && !sop.has(id)) sop.set(id, String(h.title ?? ''))
    }
  }
  const presses = new Set<string>()
  for (const t of reads.filter((x) => x.tool === 'read_sensors' && x.data)) for (const id of Object.keys((t.data!.summary ?? {}) as object)) presses.add(id)
  return (
    <section className="trace-data" aria-label="Agent Nemo 查了哪些資料">
      <h3 className="sub">Agent Nemo 查了哪些資料</h3>
      <ul>
        {[...by.entries()].map(([tool, c]) => (
          <li key={tool}><ToolIcon name={tool} /><b>{TOOL_LABEL[tool]}</b> ×{c}
            {tool === 'read_sensors' && presses.size > 0 && <span className="muted">：{[...presses].map(machineLabel).join('、')}</span>}
          </li>
        ))}
      </ul>
      {sop.size > 0 && (
        <ul className="trace-sop">
          {[...sop.entries()].map(([id, title]) => <li key={id}><b>{id}</b> {title}</li>)}
        </ul>
      )}
    </section>
  )
}

type D = Record<string, unknown>
const arr = (v: unknown): D[] => (Array.isArray(v) ? (v as D[]) : [])
