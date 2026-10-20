// Home 主控台's Agent panel (issue #8 → #37): only the final dialogue — what the supervisor typed or asked and
// the agent's replies and plan summaries. Every step behind it (state changes, the plan, tool calls, data read,
// gates, approvals) is on the Agent page (#console), linked from 「看完整過程」. The input bar keeps three modes
// (issue #10): 討論這個計劃 (POST /api/events/{id}/discuss), 下新任務 (POST /api/command), 只查不改 (POST /api/ask).
// CJ, 2026-10-20: 「對話台裡面的 agent 對話台這邊不用太多他思考工具使用的東西就是給最後的對話就可以了」.
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { AskAnswer, CommandReply, DiscussReply, Event, Line } from '../contracts'
import { machineLabel } from '../factory'
import { fmtHMS } from '../format'
import { RISK_PLAIN } from '../plain'
import { useWarRoom } from '../useWarRoom'
import { STATE_LABEL } from '../components/common'
import { ApprovalList } from './ApprovalList'
import { HomeStatus } from './HomeStatus'
import { nextUp } from './homeItems'
import { useHomeSelection } from './homeSelection'
import { PlanArea } from './PlanArea'
import { isDialogue, withAsks, type ChatItem, type Item, type LocalItem } from './transcript'
import { Row } from './TranscriptRows'
import { setReplan, useReplans } from './replanStore'
import { useTranscript } from './useTranscript'
import './agent.css'

const ASK_EXAMPLES = ['7 號機現在怎樣？', '今晚夜班缺誰？', '9 號機下一模是哪張訂單？']

export interface BarFocus { mode?: BarMode; text?: string; n: number }

/** Home 主控台 (UX brief; CJ 2026-10-20: 「我的plancanvas 那樣可以嗎計劃版然後旁邊對話匡核准列表找地方放 不要懸浮了」):
 *  one status line, then three docked columns — LEFT the ranked 核准區 list, CENTER the 計劃板 of the selected item
 *  (step cards + arrows, delivery impact, who does what, 核准 / 討論 pinned), RIGHT the Agent conversation, which
 *  follows the selected item (or 全部); 討論 puts the caret in its input in 討論這個計劃 mode. */
export function AgentTab({ asks, setAsks, picked, setPicked, onMonitor, onFull, onFocus }: {
  asks: LocalItem[]; setAsks: React.Dispatch<React.SetStateAction<LocalItem[]>>
  picked: string | null; setPicked: (id: string) => void; onMonitor?: () => void; onFull?: (eventId?: string) => void
  onFocus?: (machine?: string, line?: Line) => void
}) {
  const { state } = useWarRoom()
  const { events, list, sel, ev, now } = useHomeSelection(picked)
  const { items } = useTranscript(ev)
  const [all, setAll] = useState(false)
  const [focus, setFocus] = useState<BarFocus | null>(null)
  const chat = (mode?: BarMode, text?: string) => { setAll(false); setFocus((f) => ({ mode, text, n: (f?.n ?? 0) + 1 })) }
  const next = useMemo(() => nextUp(state.schedule, state.world, now), [state.schedule, state.world, now])
  // 「請 AI 重排」: when the task's approval card arrives, select it so the plan area shows 核准 / 討論
  const replans = useReplans()
  const arrived = Object.values(replans).find((r) => r.state === 'planning' && r.eventId && state.pending.some((p) => p.event_id === r.eventId))
  useEffect(() => {
    if (!arrived?.eventId) return
    setReplan(arrived.orderId, { state: 'ready' })
    setPicked(arrived.eventId)
  }, [arrived?.eventId, arrived?.orderId, setPicked])
  return (
    <>
    <HomeStatus onMonitor={onMonitor} onFocus={onFocus} />
    <div className="home">
      <ApprovalList items={list} selected={sel?.key ?? null} onPick={setPicked} next={next} />
      <PlanArea sel={sel} ev={ev} items={items} onDiscuss={() => chat('discuss')} onAsk={() => chat('ask')} onCommand={(t) => chat('command', t)} />
      <Console ev={ev} events={events} onPick={setPicked} items={items} asks={asks} setAsks={setAsks} all={all} setAll={setAll} focus={focus} onFull={onFull} />
    </div>
    </>
  )
}

function Console({ ev, events, onPick, items, asks, setAsks, all: allView, setAll, focus, onFull }: {
  ev: Event | null; events: Event[]; onPick: (id: string) => void; items: Item[]
  asks: LocalItem[]; setAsks: React.Dispatch<React.SetStateAction<LocalItem[]>>
  all: boolean; setAll: (v: boolean) => void; focus: BarFocus | null; onFull?: (eventId?: string) => void
}) {
  const { state } = useWarRoom()
  const talk = useMemo(() => withAsks(items.filter(isDialogue), asks), [items, asks])
  const steps = items.length - items.filter(isDialogue).length
  const scroller = useRef<HTMLDivElement>(null)
  const [pinned, setPinned] = useState(true)
  const onScroll = () => {
    const el = scroller.current
    if (el) setPinned(el.scrollHeight - el.scrollTop - el.clientHeight < 40)
  }
  const last = talk[talk.length - 1]
  const tail = `${allView}:${events.length}:${talk.length}:${last?.key}:${last?.k === 'ask' ? !!last.answer : ''}:${last?.k === 'chat' ? last.pending : ''}`
  useLayoutEffect(() => {
    const el = scroller.current
    if (el && pinned) el.scrollTop = el.scrollHeight
  }, [tail, pinned])
  const jump = () => { const el = scroller.current; if (el) { el.scrollTop = el.scrollHeight; setPinned(true) } }
  const open = !!ev && ev.state !== 'closed' && ev.state !== 'safe_stop'
  const waiting = open && ev.state === 'await_approval'
  const running = items.some((i) => i.k === 'tool' && i.status === 'running')
  const full = (id?: string) => (e: React.MouseEvent) => { if (onFull) { e.preventDefault(); onFull(id) } }
  return (
    <section className="console panel" aria-label="Agent 對話">
      <header className="con-h">
        <div className="con-title">
          <h2>Agent 對話</h2>
          <div className="seg sm con-view" role="group" aria-label="對話範圍">
            <button className={!allView ? 'on' : ''} aria-pressed={!allView} onClick={() => setAll(false)} disabled={!ev} data-testid="con-follow">
              {ev ? `${ev.source === 'supervisor' ? '主管指令' : machineLabel(ev.machine_id)}・${fmtHMS(ev.opened_at).slice(0, 5)}` : '此事項'}</button>
            <button className={allView ? 'on' : ''} aria-pressed={allView} onClick={() => setAll(true)} data-testid="con-all">全部</button>
          </div>
          {ev && !allView && <span className={`state-pill st-${ev.state}`}>{STATE_LABEL[ev.state]}</span>}
          <a className="con-full" href={`#console${ev && !allView ? `?event=${encodeURIComponent(ev.id)}` : ''}`} onClick={full(allView ? undefined : ev?.id)} data-testid="see-full">看完整過程 →</a>
        </div>
      </header>
      <div className="con-body" ref={scroller} onScroll={onScroll} data-testid="transcript">
        {allView && events.slice().reverse().map((e) => <EventFeed key={e.id} ev={e} onPick={onPick} />)}
        {allView && !events.length && <p className="muted con-empty">目前沒有事件。下方可直接提問。</p>}
        {!allView && ev && steps > 0 && (
          <p className="con-steps muted small">AI 指揮官處理這件事做了 {steps} 個步驟（狀態、計畫、工具、把關、核准）。<a href="#console" onClick={full(ev.id)}>看完整過程</a></p>
        )}
        {!allView && !talk.length && <p className="muted con-empty">{ev ? '這件事還沒有對話。下方可以討論計劃、下新任務或提問。' : '目前沒有事件。下方可直接提問。'}</p>}
        {(allView ? talk.filter((it) => it.k === 'ask' || (it.k === 'chat' && it.pending)) : talk).map((it) => <Row key={it.key} it={it} ev={ev} nth={0} brief />)}
        {!allView && open && (
          <div className={`con-live ${waiting ? 'wait' : ''}`} role="status" aria-live="polite">
            {waiting ? <><i className="pulse" />等待主管核准…（{state.pending.filter((p) => p.event_id === ev.id).map((p) => RISK_PLAIN[p.risk]).join('、') || '核准卡在中間'}）</>
              : <><i className="spin" />AI 指揮官{STATE_LABEL[ev.state]}中{running ? '・工具執行中' : ''}…</>}
          </div>
        )}
      </div>
      {!pinned && <button className="con-jump" onClick={jump}>↓ 跳到最新</button>}
      <ChatBar ev={ev} waiting={!!waiting && state.pending.some((p) => p.event_id === ev?.id)} setAsks={setAsks} onPick={onPick} focus={focus} />
    </section>
  )
}

/** 全部: every event's dialogue in one feed, oldest first, each under a heading that selects it. */
function EventFeed({ ev, onPick }: { ev: Event; onPick: (id: string) => void }) {
  const { items } = useTranscript(ev)
  const rows = items.filter(isDialogue)
  return (
    <div className="feed-ev">
      <button type="button" className="feed-ev-h" onClick={() => onPick(ev.id)}>
        {ev.source === 'supervisor' ? '主管指令' : machineLabel(ev.machine_id)}・{fmtHMS(ev.opened_at).slice(0, 5)}・{STATE_LABEL[ev.state]}
      </button>
      {rows.map((it) => <Row key={it.key} it={it} ev={ev} nth={0} brief />)}
    </div>
  )
}

let askSeq = 0

type BarMode = 'discuss' | 'command' | 'ask'
const MODES: { id: BarMode; label: string; hint: string }[] = [
  { id: 'discuss', label: '討論這個計劃', hint: '提出現場條件，AI 會重排並發新版核准卡' },
  { id: 'command', label: '下新任務', hint: '交辦一件事，走計畫 → 把關 → 核准' },
  { id: 'ask', label: '只查不改', hint: '答案只來自感測器、訂單班表與 SOP' },
]
const EXAMPLES: Record<BarMode, string[]> = {
  discuss: ['9 號機今晚要保養', '不要跨組調人', '為什麼用 9 號機？'],
  command: ['通知維修組明早檢查 3 號機', '9 號機明天要保養，重排'],
  ask: ASK_EXAMPLES,
}
const PLACEHOLDER: Record<BarMode, string> = {
  discuss: '跟 AI 討論等待核准的計劃，例如：9 號機今晚要保養（Enter 送出）',
  command: '交辦新任務，例如：通知維修組明早檢查 3 號機（Enter 送出）',
  ask: '問戰情室，例如：9 號機下一模是哪張訂單？（Enter 送出）',
}

function ChatBar({ ev, waiting, setAsks, onPick, focus }: {
  ev: Event | null; waiting: boolean; setAsks: React.Dispatch<React.SetStateAction<LocalItem[]>>; onPick: (id: string) => void
  focus: BarFocus | null
}) {
  const { transport, state } = useWarRoom()
  const [q, setQ] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [picked, setPicked] = useState<BarMode | null>(null)
  const input = useRef<HTMLTextAreaElement>(null)
  // 討論 / 問 AI / 請 AI 重排 in the plan area: switch the bar's mode, prefill, and put the caret here
  const [seen, setSeen] = useState(0)
  if (focus && focus.n !== seen) {
    setSeen(focus.n)
    if (focus.mode) setPicked(focus.mode)
    if (focus.text) setQ(focus.text)
  }
  useEffect(() => { if (focus?.n) setTimeout(() => input.current?.focus(), 60) }, [focus?.n])
  const mode: BarMode = picked === 'discuss' && !waiting ? 'ask' : picked ?? (waiting ? 'discuss' : 'ask')
  const can = mode === 'ask' ? !!transport?.ask : mode === 'command' ? !!transport?.command : !!transport?.discuss && waiting
  const lanDown = state.snapshot?.header.network === 'lan_down'
  const ft = state.clock?.factory_time ?? state.snapshot?.header.factory_time ?? ''
  const chat = (role: ChatItem['role'], kind: ChatItem['kind'], text: string, key: string, n: number, ts: string): ChatItem => ({
    k: 'chat', key, ts, seq: 1e9 + n, role, kind, text, by: 'supervisor', intent: null, version: null,
    changes: [], constraints: [], classifiedBy: null, fellBack: false, pending: true,
  })
  const send = async (raw: string) => {
    const text = raw.trim()
    if (!text || busy || !can || !transport) return
    const n = ++askSeq
    setQ('')
    if (mode === 'ask' && transport.ask) {
      const key = `ask-${n}`
      setAsks((xs) => [...xs, { k: 'ask', key, ts: ft || new Date().toISOString(), seq: 1e9 + n, question: text, answer: null, error: null }])
      setBusy('查詢中…')
      try {
        const a: AskAnswer = await transport.ask(text)
        setAsks((xs) => xs.map((x) => (x.key === key && x.k === 'ask' ? { ...x, answer: a, ts: a.factory_time || x.ts } : x)))
      } catch (e) {
        setAsks((xs) => xs.map((x) => (x.key === key && x.k === 'ask' ? { ...x, error: `查詢失敗：${String(e)}` } : x)))
      } finally { setBusy(null) }
      return
    }
    // discuss / command: shown at once, then replaced by the server's audit rows (the record of truth)
    const keys = [`me-${n}`, `ag-${n}`]
    setAsks((xs) => {
      const ts = ft || new Date().toISOString()
      return [...xs, chat('me', mode === 'command' ? 'command' : 'discuss', text, keys[0], n, ts),
        { ...chat('agent', 'reply', mode === 'command' ? 'AI 指揮官接下任務中…' : 'AI 指揮官判讀中；若要改計劃，會重新求解排程並發新版核准卡…', keys[1], n + 0.5, ts), by: 'agent' }]
    })
    setBusy(mode === 'command' ? '交辦中…' : '重新規劃中…')
    const drop = () => setAsks((xs) => xs.filter((x) => !keys.includes(x.key)))
    try {
      if (mode === 'command' && transport.command) {
        const r: CommandReply = await transport.command(text)
        if (r.event_id) { drop(); onPick(r.event_id) } else {
          // not a task (e.g. a greeting): the reply stays here, no event was opened
          setAsks((xs) => xs.map((x) => (x.key === keys[1] && x.k === 'chat' ? { ...x, pending: false, text: r.reply } : x)))
        }
      } else if (ev && transport.discuss) {
        const r: DiscussReply = await transport.discuss(ev.id, text)
        void r
        drop()
      }
    } catch (e) {
      setAsks((xs) => xs.map((x) => (x.key === keys[1] && x.k === 'chat' ? { ...x, pending: false, text: `送出失敗：${String(e)}` } : x)))
    } finally { setBusy(null) }
  }
  return (
    <form className="askbar" onSubmit={(e) => { e.preventDefault(); void send(q) }} data-mode={mode}>
      <div className="askbar-modes" role="radiogroup" aria-label="輸入模式">
        {MODES.map((m) => {
          const off = m.id === 'discuss' && !waiting
          return (
            <button type="button" key={m.id} role="radio" aria-checked={mode === m.id} className={mode === m.id ? 'on' : ''} disabled={off || !!busy}
              title={off ? '目前沒有等待核准的計劃' : m.hint} onClick={() => setPicked(m.id)} data-testid={`mode-${m.id}`}>{m.label}</button>
          )
        })}
        <span className="askbar-hint">{MODES.find((m) => m.id === mode)?.hint}{mode === 'discuss' && ev ? `・${machineLabel(ev.machine_id)}` : ''}</span>
      </div>
      <div className="askbar-ex">
        {EXAMPLES[mode].map((x) => <button type="button" key={x} disabled={!can || !!busy} onClick={() => void send(x)}>{x}</button>)}
      </div>
      <div className="askbar-in">
        <textarea ref={input} rows={1} maxLength={300} value={q} disabled={!can || !!busy} aria-label={MODES.find((m) => m.id === mode)?.label}
          data-testid="chat-input"
          placeholder={can ? (lanDown ? 'LAN 中斷時請在現場觸控螢幕操作' : PLACEHOLDER[mode]) : transport?.mode === 'mock' ? '模擬資料模式不支援，需連線後端' : '目前沒有等待核准的計劃'}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(q) } }} />
        <button type="submit" className="btn-send" disabled={!can || !!busy || !q.trim()} data-testid="chat-send">{busy ?? '送出'}</button>
      </div>
    </form>
  )
}
