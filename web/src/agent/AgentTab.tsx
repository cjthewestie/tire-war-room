// Home 主控台's Agent panel (issue #8 → #37): only the final dialogue — what the supervisor typed or asked and
// the agent's replies and plan summaries. Every step behind it (state changes, the plan, tool calls, data read,
// gates, approvals) is on the Agent page (#console), linked from 「看完整過程」. The input bar keeps three modes
// (issue #10): 討論這個計劃 (POST /api/events/{id}/discuss), 下新任務 (POST /api/command), 只查不改 (POST /api/ask).
// CJ, 2026-10-20: 「對話台裡面的 agent 對話台這邊不用太多他思考工具使用的東西就是給最後的對話就可以了」.
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { Event, Line } from '../contracts'
import { machineLabel } from '../factory'
import { fmtHMS } from '../format'
import { RISK_PLAIN } from '../plain'
import { useWarRoom } from '../useWarRoom'
import { STATE_LABEL } from '../components/common'
import { ApprovalList } from './ApprovalList'
import { HomeOverview } from './HomeOverview'
import { HomeStatus } from './HomeStatus'
import { nextUp } from './homeItems'
import { useHomeSelection } from './homeSelection'
import { PlanArea } from './PlanArea'
import { isDialogue, withAsks, type Item, type LocalItem } from './transcript'
import { Row } from './TranscriptRows'
import { setReplan, useReplans } from './replanStore'
import { ChatBar, type BarFocus, type BarMode } from './ChatBar'
import { useTranscript } from './useTranscript'
import './agent.css'

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
  // the task ended without a card (nothing to change, or a safe stop): say so on the item instead of planning forever
  const ended = Object.values(replans).map((r) => ({ r, ev: r.state === 'planning' ? events.find((e) => e.id === r.eventId) : undefined }))
    .find((x) => x.ev && (x.ev.state === 'closed' || x.ev.state === 'safe_stop'))
  useEffect(() => {
    if (ended?.ev) setReplan(ended.r.orderId, { state: 'closed', outcome: ended.ev.outcome ?? null })
  }, [ended?.ev, ended?.r.orderId])
  return (
    <>
    <HomeStatus onMonitor={onMonitor} onFocus={onFocus} />
    <div className="home">
      {!sel && <HomeOverview onMonitor={onMonitor} onFull={onFull} />}{/* nothing to decide: 今日工廠概況 in the plan column (grid-area) */}
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
