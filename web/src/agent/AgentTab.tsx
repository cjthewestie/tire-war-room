// Agent 對話台 (issue #8, part A): a Claude-Code-style live transcript of what the AI commander did for
// one event (state changes, the plan the model returned, every tool call and result, the gate, the
// supervisor), plus 問戰情室 at the bottom (read-only questions, POST /api/ask).
import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { AskAnswer, Event } from '../contracts'
import { machineLabel } from '../factory'
import { fmtHMS } from '../format'
import { RISK_PLAIN, hypName, plain } from '../plain'
import { primaryEvent } from '../store'
import { TOOL_LABEL, resultText, stepLabel } from '../stepText'
import { useWarRoom } from '../useWarRoom'
import { STATE_LABEL } from '../components/common'
import { ApprovalVisual, PlanFlow, ToolChips } from './ApprovalVisual'
import { currentRoundTools, planNodes, toolUses } from './approvalView'
import { ToolIcon } from './ToolIcon'
import {
  expectationText, inFilter, thinkContext, withAsks,
  type ApprovalItem, type AskItem, type Filter, type GateItem, type Item, type ThinkItem, type ToolItem,
} from './transcript'
import { useTranscript } from './useTranscript'
import './agent.css'

const MAX_ROWS = 500
const FILTERS: { id: Filter; label: string }[] = [{ id: 'all', label: '全部' }, { id: 'tool', label: '工具' }, { id: 'think', label: '思考' }, { id: 'gate', label: '把關' }]
const ASK_EXAMPLES = ['7 號機現在怎樣？', '今晚夜班缺誰？', '9 號機下一模是哪張訂單？']
const PHASE: Record<string, string> = { plan: '規劃', replan: '重新規劃' }
const VERDICT: Record<string, string> = { allow: '放行', needs_approval: '需主管核准', reject: '拒絕' }
const DECISION: Record<string, string> = { approve: '核准', modify: '修改', reject: '駁回' }
const VIA: Record<string, string> = { phone: '手機', touchscreen: '現場觸控螢幕', dashboard: '戰情室', auto: '自動' }

export function AgentTab({ asks, setAsks, picked, setPicked }: {
  asks: AskItem[]; setAsks: React.Dispatch<React.SetStateAction<AskItem[]>>
  picked: string | null; setPicked: (id: string) => void
}) {
  const { state } = useWarRoom()
  const events = useMemo(() => [...(state.snapshot?.events ?? [])].sort((a, b) => (a.opened_at < b.opened_at ? 1 : -1)), [state.snapshot?.events])
  const ev = events.find((e) => e.id === picked) ?? primaryEvent(events)
  const { items } = useTranscript(ev)
  const pending = ev ? state.pending.filter((p) => p.event_id === ev.id) : []
  const tools = useMemo(() => items.filter((i): i is ToolItem => i.k === 'tool'), [items])
  return (
    <div className="agent">
      <Console ev={ev} events={events} onPick={setPicked} items={items} asks={asks} setAsks={setAsks} />
      <aside className="agent-side" aria-label="計畫與核准">
        {pending.map((p) => <ApprovalVisual key={p.id} req={p} ev={ev} items={items} layout="side" />)}
        {!pending.length && ev && <EventSummary ev={ev} items={items} tools={tools} />}
        {!ev && <div className="panel agent-empty"><p className="calm-title">全廠正常</p><p className="muted">有異常時，AI 指揮官的計畫、調用的工具和待核准事項會出現在這裡。你仍可在左邊提問（只查不改）。</p></div>}
      </aside>
    </div>
  )
}

function EventSummary({ ev, items, tools }: { ev: Event; items: Item[]; tools: ToolItem[] }) {
  const nodes = planNodes(ev, currentRoundTools(items))
  const uses = toolUses(ev, tools)
  const closed = ev.state === 'closed' || ev.state === 'safe_stop'
  return (
    <section className="apv calm" data-testid="event-summary">
      <header className="apv-h">
        <div className="apv-tags"><span className="apv-round">{machineLabel(ev.machine_id)}</span><span className={`state-pill st-${ev.state}`}>{STATE_LABEL[ev.state]}</span></div>
        <h3 className="apv-title">{plain(ev.alert_card?.title ?? ev.anomaly.message)}</h3>
        {closed && ev.outcome && <p className="apv-reason">結果：{plain(ev.outcome)}</p>}
        {!closed && <p className="apv-reason">目前沒有等待核准的事項。</p>}
      </header>
      {nodes.length > 0 && <div className="apv-b"><h4 className="apv-bh"><span className="apv-n">1</span>處理計畫</h4><PlanFlow nodes={nodes} /></div>}
      {uses.length > 0 && <div className="apv-b"><h4 className="apv-bh"><span className="apv-n">2</span>調用的工具</h4><ToolChips uses={uses} /></div>}
    </section>
  )
}

function Console({ ev, events, onPick, items, asks, setAsks }: {
  ev: Event | null; events: Event[]; onPick: (id: string) => void; items: Item[]
  asks: AskItem[]; setAsks: React.Dispatch<React.SetStateAction<AskItem[]>>
}) {
  const { state } = useWarRoom()
  const [filter, setFilter] = useState<Filter>('all')
  const all = useMemo(() => withAsks(items, asks), [items, asks])
  const [cap, setCap] = useState(MAX_ROWS)
  const matching = all.filter((i) => inFilter(i, filter))
  const hidden = Math.max(0, matching.length - cap)
  const shown = hidden ? matching.slice(hidden) : matching
  const scroller = useRef<HTMLDivElement>(null)
  const [pinned, setPinned] = useState(true)
  const onScroll = () => {
    const el = scroller.current
    if (el) setPinned(el.scrollHeight - el.scrollTop - el.clientHeight < 40)
  }
  const last = shown[shown.length - 1]
  const tail = `${shown.length}:${last?.key}:${last?.k === 'tool' ? last.status : ''}:${last?.k === 'ask' ? !!last.answer : ''}`
  useLayoutEffect(() => {
    const el = scroller.current
    if (el && pinned) el.scrollTop = el.scrollHeight
  }, [tail, pinned])
  const jump = () => { const el = scroller.current; if (el) { el.scrollTop = el.scrollHeight; setPinned(true) } }
  const open = !!ev && ev.state !== 'closed' && ev.state !== 'safe_stop'
  const waiting = open && ev.state === 'await_approval'
  const running = items.some((i) => i.k === 'tool' && i.status === 'running')
  // the n-th plan block maps to the n-th round
  const thinkIdx = new Map<string, number>()
  let n = 0
  for (const i of items) if (i.k === 'think' && i.stepIds.length) thinkIdx.set(i.key, n++)
  return (
    <section className="console panel" aria-label="Agent 對話台">
      <header className="con-h">
        <div className="con-title">
          <h2><span className="num">◆</span>Agent 對話台</h2>
          {events.length > 1 ? (
            <select className="con-pick" value={ev?.id ?? ''} onChange={(e) => onPick(e.target.value)} aria-label="選擇事件">
              {events.map((e) => <option key={e.id} value={e.id}>{machineLabel(e.machine_id)}・{STATE_LABEL[e.state]}・{fmtHMS(e.opened_at).slice(0, 5)}</option>)}
            </select>
          ) : ev ? <span className="con-ev">{machineLabel(ev.machine_id)}・{fmtHMS(ev.opened_at).slice(0, 5)}</span> : null}
          {ev && <span className={`state-pill st-${ev.state}`}>{STATE_LABEL[ev.state]}</span>}
        </div>
        <div className="con-filters" role="group" aria-label="篩選">
          {FILTERS.map((f) => (
            <button key={f.id} className={filter === f.id ? 'on' : ''} aria-pressed={filter === f.id} onClick={() => setFilter(f.id)}>
              {f.label}<small>{all.filter((i) => inFilter(i, f.id)).length}</small>
            </button>
          ))}
        </div>
      </header>
      <div className="con-body" ref={scroller} onScroll={onScroll} data-testid="transcript">
        {hidden > 0 && <button className="con-more" onClick={() => setCap((c) => c + MAX_ROWS)}>顯示較早的 {Math.min(hidden, MAX_ROWS)} 筆（共 {hidden} 筆未顯示）</button>}
        {!shown.length && <p className="muted con-empty">{ev ? (filter === 'all' ? '讀取稽核紀錄中…' : '這個篩選沒有紀錄') : '目前沒有事件。下方可直接提問。'}</p>}
        {shown.map((it) => <Row key={it.key} it={it} ev={ev} nth={it.k === 'think' ? thinkIdx.get(it.key) ?? 0 : 0} />)}
        {open && (filter === 'all') && (
          <div className={`con-live ${waiting ? 'wait' : ''}`} role="status" aria-live="polite">
            {waiting ? <><i className="pulse" />等待主管核准…（{state.pending.filter((p) => p.event_id === ev.id).map((p) => RISK_PLAIN[p.risk]).join('、') || '核准卡在右側'}）</>
              : <><i className="spin" />AI 指揮官{STATE_LABEL[ev.state]}中{running ? '・工具執行中' : ''}…</>}
          </div>
        )}
      </div>
      {!pinned && <button className="con-jump" onClick={jump}>↓ 跳到最新</button>}
      <AskBar setAsks={setAsks} />
    </section>
  )
}

function Row({ it, ev, nth }: { it: Item; ev: Event | null; nth: number }) {
  switch (it.k) {
    case 'state':
      return <div className="r-state" data-kind="state"><span>{STATE_LABEL[it.to as keyof typeof STATE_LABEL] ?? it.to}</span>{it.reason && <small>{reasonText(it.reason)}</small>}<time>{fmtHMS(it.ts)}</time></div>
    case 'think': return <ThinkRow it={it} ev={ev} nth={nth} />
    case 'tool': return <ToolRow it={it} />
    case 'gate': return <GateRow it={it} />
    case 'blocked':
      return (
        <div className="r-line r-blocked" data-kind="blocked"><ToolIcon name="blocked" />
          <span><b>已攔下</b>{it.tool ? ` ${TOOL_LABEL[it.tool as keyof typeof TOOL_LABEL] ?? it.tool}` : ''}{it.stepId ? `（${stepLabel(it.stepId)}）` : ''}：{it.reason}</span><time>{fmtHMS(it.ts)}</time></div>
      )
    case 'approval': return <ApprovalRow it={it} />
    case 'safety':
      return <div className="r-line r-safety" data-kind="safety"><ToolIcon name="safety" /><span><b>安全核心</b>　{plain(it.message)}<small>（不經 AI，毫秒級）</small></span><time>{fmtHMS(it.ts)}</time></div>
    case 'anomaly':
      return <div className="r-line r-anom" data-kind="anomaly"><ToolIcon name="safety" /><span><b>事件偵測</b>　{plain(it.message)}</span><time>{fmtHMS(it.ts)}</time></div>
    case 'note':
      return <div className={`r-line r-note ${it.tone}`} data-kind="note"><span className="r-dot" /><span>{plain(it.text)}</span><time>{fmtHMS(it.ts)}</time></div>
    case 'ask': return <AskRow it={it} />
  }
}

function ThinkRow({ it, ev, nth }: { it: ThinkItem; ev: Event | null; nth: number }) {
  const { hyp, steps } = thinkContext(ev, it, nth)
  const who = it.mode === 'llm' ? `Nemotron${it.model ? `（${it.model}）` : ''}` : it.mode === 'mock' ? '規則模式指揮官' : 'AI 指揮官'
  const exp = expectationText(hyp)
  return (
    <article className="r-think" data-kind="think" data-testid="think-block">
      <header><ToolIcon name="think" /><b>{who} {PHASE[it.phase] ?? it.phase}</b>
        {it.totalMs != null && <small>{Math.round(it.totalMs)} ms</small>}{it.tokens != null && <small>{it.tokens} tokens</small>}<time>{fmtHMS(it.ts)}</time></header>
      <p className="r-think-note">{thinkNote(it)}</p>
      {it.error && <p className="r-warn">{it.source === 'mock_fallback' ? '模型輸出未通過檢查，改用規則備援計畫：' : '呼叫失敗：'}{it.error}</p>}
      {it.hypothesisId && (
        <div className="r-hyp">
          <span className="r-hyp-id">{hypName(it.hypothesisId)}</span>
          <span>{hyp ? plain(hyp.statement) : it.causeCode ?? ''}</span>
          {hyp && <span className={`r-hyp-st st-${hyp.status}`}>{hyp.status === 'rejected' ? '已被讀值推翻' : hyp.status === 'confirmed' ? '已驗證成立' : '待驗證'}</span>}
        </div>
      )}
      {exp && <p className="r-sub">預期：{exp}</p>}
      {hyp && hyp.evidence.length > 0 && <ul className="r-ev">{hyp.evidence.slice(0, 3).map((e, i) => <li key={i}>{plain(e)}</li>)}</ul>}
      {it.stepIds.length > 0 && (
        <ol className="r-steps">
          {(steps.length ? steps : it.stepIds.map((id) => ({ id, tool: null, purpose: '' }))).map((s) => (
            <li key={s.id}><b>{s.id}</b>{s.tool && <span className="r-steps-tool"><ToolIcon name={s.tool} />{TOOL_LABEL[s.tool]}</span>}<span className="muted">{plain(s.purpose)}</span></li>
          ))}
        </ol>
      )}
      {it.rationale && <p className="r-sub">理由：{plain(it.rationale)}</p>}
      {it.safeStop && <p className="r-warn">指揮官建議安全停止</p>}
    </article>
  )
}

/** Say where the plan came from: the model (reasoning off), the rule fallback, or the rule-mode commander. */
function thinkNote(it: ThinkItem): string {
  if (it.mode === 'llm' && it.source === 'mock_fallback') return '模型輸出未採用：以下計畫由規則備援產生，不是模型的思考過程。'
  if (it.mode === 'llm') return '推理模式關閉：以下是模型回傳的計畫與假設，不是隱藏的思考過程。'
  if (it.mode === 'mock') return '規則模式：沒有呼叫模型，以下計畫與假設由規則式指揮官產生。'
  return '以下是這一輪的計畫與假設（依事件紀錄重建），不是隱藏的思考過程。'
}

function ToolRow({ it }: { it: ToolItem }) {
  let line = ''
  if (it.status === 'ok' && it.data) { try { line = resultText(it.tool, it.args, it.data) } catch { line = '' } }
  if (it.status === 'failed') line = `失敗：${it.error ?? ''}`
  const ask = it.phase === 'ASK'
  return (
    <details className={`r-tool ts-${it.status}`} data-kind="tool" data-tool={it.tool}>
      <summary>
        <ToolIcon name={it.tool} />
        <b>{TOOL_LABEL[it.tool] ?? it.tool}</b>
        <span className="r-step">{it.stepId && !ask ? stepLabel(it.stepId) : ask ? '提問' : '情境'}</span>
        {it.attempt > 1 && <span className="r-retry">重試 #{it.attempt}</span>}
        <span className="r-res">{it.status === 'running' ? <><i className="spin" />執行中…</> : <>{plain(line) || '完成'}{it.partial && <em className="r-partial">（完整結果同步中…）</em>}</>}</span>
        {it.durationMs != null && <small className="r-ms">{fmtMs(it.durationMs)}</small>}
        <time>{fmtHMS(it.ts)}</time>
      </summary>
      <div className="r-json">
        <details><summary>參數</summary><pre>{JSON.stringify(it.args, null, 2)}</pre></details>
        {it.data && <details><summary>結果（原始資料）</summary><pre>{JSON.stringify(it.data, null, 2)}</pre></details>}
        {it.error && <pre className="r-err">{it.error}</pre>}
      </div>
    </details>
  )
}
function fmtMs(n: number): string { return n >= 1000 ? `${(n / 1000).toFixed(1)} s` : `${Math.round(n)} ms` }

function GateRow({ it }: { it: GateItem }) {
  const v = it.verdict ?? (it.dropped ? 'reject' : '')
  return (
    <div className={`r-line r-gate gv-${v}`} data-kind="gate"><ToolIcon name="gate" />
      <span><b>動作把關</b>　{it.stepId ? stepLabel(it.stepId) : ''}{it.tool ? ` ${TOOL_LABEL[it.tool as keyof typeof TOOL_LABEL] ?? it.tool}` : ''}
        {it.risk && <span className={`risk r-${it.risk}`}>{it.risk}</span>}
        <span className="r-verdict">{VERDICT[v] ?? v}</span>
        {it.reasons.filter((r) => !/approval required$/.test(r)).length > 0 && <small>（{it.reasons.filter((r) => !/approval required$/.test(r)).join('；')}）</small>}
      </span><time>{fmtHMS(it.ts)}</time></div>
  )
}

function ApprovalRow({ it }: { it: ApprovalItem }) {
  if (it.phase === 'request') {
    return <div className="r-line r-appr" data-kind="approval"><ToolIcon name="human" /><span><b>送主管核准</b>{it.round ? `（第 ${it.round} 輪）` : ''}{it.risk && <span className={`risk r-${it.risk}`}>{it.risk}</span>}　{plain(it.title)}</span><time>{fmtHMS(it.ts)}</time></div>
  }
  const who = !it.by || it.by === 'supervisor' ? '主管' : it.by === 'system' ? '系統' : it.by
  return (
    <div className={`r-line r-appr dec-${it.decision}`} data-kind="approval"><ToolIcon name="human" />
      <span><b>{who}{DECISION[it.decision ?? ''] ?? it.decision}</b>{it.optionId ? ` 方案 ${it.optionId}` : ''}{it.via ? `（${VIA[it.via] ?? it.via}）` : ''}{it.reason ? `　${plain(it.reason)}` : ''}</span><time>{fmtHMS(it.ts)}</time></div>
  )
}

function AskRow({ it }: { it: AskItem }) {
  const a = it.answer
  return (
    <div className="r-ask" data-kind="ask">
      <div className="r-q"><span>{it.question}</span><small>主管・{fmtHMS(it.ts)}</small></div>
      <div className="r-a" data-testid={a ? 'ask-answer' : undefined}>
        <header><ToolIcon name="think" /><b>問戰情室</b><span className="ro">只查不改</span>{a && <small>{a.id}・{Math.round(a.total_ms)} ms{a.mode === 'mock' || a.fell_back_to_mock ? '・規則模式' : ''}</small>}</header>
        {!a && !it.error && <p className="muted"><i className="spin" />查詢中…</p>}
        {it.error && <p className="err">{it.error}</p>}
        {a && a.steps.map((s, i) => (
          <details key={i} className={`r-tool ts-${s.ok ? 'ok' : 'failed'}`} data-kind="tool" data-tool={s.tool}>
            <summary><ToolIcon name={s.tool} /><b>{TOOL_LABEL[s.tool as keyof typeof TOOL_LABEL] ?? s.tool}</b><span className="r-step">Q{i + 1}</span><span className="r-res">{s.ok ? s.summary : `失敗：${s.error ?? ''}`}</span></summary>
            <div className="r-json"><details><summary>參數</summary><pre>{JSON.stringify(s.args, null, 2)}</pre></details></div>
          </details>
        ))}
        {a && a.intercepted.map((x, i) => <div key={`x${i}`} className="r-line r-blocked"><ToolIcon name="blocked" /><span><b>已攔下</b> {x.tool}：{x.reason}</span></div>)}
        {a && <p className="r-answer">{a.answer}</p>}
      </div>
    </div>
  )
}

const REASON: Record<string, string> = {
  'detector wake-up': '事件偵測喚醒', 'gather sensor window, prediction and SOP': '讀感測器、預測與 SOP', 'ask commander': '請指揮官規劃',
  'run read/compute steps': '執行讀取／計算步驟', 'check write steps': '檢查會改變現場的步驟', 'approved by supervisor': '主管已核准',
  'compare expectation with readings (code)': '程式比對預期與讀值', 'rejected by supervisor': '主管已駁回',
}
function reasonText(r: string): string {
  if (REASON[r]) return REASON[r]
  const m = /^(R\d) approval$/.exec(r)
  if (m) return `${m[1]} 需主管核准`
  return plain(r)
}

let askSeq = 0

function AskBar({ setAsks }: { setAsks: React.Dispatch<React.SetStateAction<AskItem[]>> }) {
  const { transport, state } = useWarRoom()
  const [q, setQ] = useState('')
  const [busy, setBusy] = useState(false)
  const can = !!transport?.ask
  const lanDown = state.snapshot?.header.network === 'lan_down'
  const ft = state.clock?.factory_time ?? state.snapshot?.header.factory_time ?? ''
  const send = async (question: string) => {
    const text = question.trim()
    if (!transport?.ask || !text || busy) return
    const n = ++askSeq
    const key = `ask-${n}`
    setAsks((xs) => [...xs, { k: 'ask', key, ts: ft || new Date().toISOString(), seq: 1e9 + n, question: text, answer: null, error: null }])
    setQ(''); setBusy(true)
    try {
      const a: AskAnswer = await transport.ask(text)
      setAsks((xs) => xs.map((x) => (x.key === key ? { ...x, answer: a, ts: a.factory_time || x.ts } : x)))
    } catch (e) {
      setAsks((xs) => xs.map((x) => (x.key === key ? { ...x, error: `查詢失敗：${String(e)}` } : x)))
    } finally { setBusy(false) }
  }
  return (
    <form className="askbar" onSubmit={(e) => { e.preventDefault(); void send(q) }}>
      <div className="askbar-ex">
        <span className="ro" title="答案只來自感測器、訂單班表與 SOP；停機、重排、調人請走事件核准流程">只查不改</span>
        {ASK_EXAMPLES.map((x) => <button type="button" key={x} disabled={!can || busy} onClick={() => void send(x)}>{x}</button>)}
      </div>
      <div className="askbar-in">
        <textarea rows={1} maxLength={300} value={q} disabled={!can || busy} aria-label="問戰情室"
          placeholder={can ? (lanDown ? 'LAN 中斷時請在現場觸控螢幕提問' : '問戰情室，例如：9 號機下一模是哪張訂單？（Enter 送出）') : '模擬資料模式不支援提問，需連線後端'}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(q) } }} />
        <button type="submit" className="btn-send" disabled={!can || busy || !q.trim()}>{busy ? '查詢中…' : '送出'}</button>
      </div>
    </form>
  )
}
