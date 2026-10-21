// The 主控台 conversation, plain (CJ, 2026-10-20: 「單純一點好嗎」): supervisor bubble ↔ 主 agent bubble, nothing else.
// A reply's tool calls collapse to one small grey line (「查了 SOP-OM-01・派給訂單處理」, click to expand); a reply the
// rules wrote because the model failed gets one amber line. No tags, no colours, no labels. Details live on the Agent page.
//
// MainThread: the 主 agent's whole thread across subagents — its turns (audit kind=system actor=main_agent orch=chat,
// backfilled once from GET /api/audit and then live), its dispatches / reports / interrupts, and what is being sent now.
// SubProgress: one subagent's progress in the same panel (state, tools with results, worker tasks, 中斷).
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import type { Event } from '../contracts'
import { fmtHM, ms } from '../format'
import { plain } from '../plain'
import { TOOL_LABEL, resultText } from '../stepText'
import { STATE_LABEL } from '../components/common'
import { TaskList } from '../components/TaskList'
import { useWarRoom } from '../useWarRoom'
import { plainStatus } from './subagents'
import type { ToolItem } from './transcript'
import type { Bubble } from './mainThread'
import { useTranscript } from './useTranscript'

/** `extra`: one node placed among the bubbles by time (the 待核准 notice / briefing: bubbles at or before `at` come first).
 *  `guide`: shown instead of the plain `empty` text when there is nothing yet. `follow`: the 主控台 behaviour — stay at
 *  the newest message only while the reader is at the bottom, a 「跳到最新」 button otherwise, and the order hint. */
export function Bubbles({ items, empty, extra = null, guide = null, follow = false }: {
  items: Bubble[]; empty: string; extra?: { at: number; node: ReactNode; alignTop?: boolean } | null; guide?: ReactNode; follow?: boolean
}) {
  const scroller = useRef<HTMLDivElement>(null)
  const extraBox = useRef<HTMLDivElement>(null)
  const [pinned, setPinned] = useState(true)
  const skip = useRef(false)      // our own scroll (to the newest / to a briefing's heading) is not the reader leaving the bottom
  const last = items[items.length - 1]
  const at = extra && !isNaN(extra.at) ? extra.at : Infinity
  let cut = items.length
  while (cut > 0 && ms(items[cut - 1].ts) > at) cut--
  const before = extra ? items.slice(0, cut) : items
  const after = extra ? items.slice(cut) : []
  const onScroll = () => {
    const el = scroller.current
    if (skip.current) { skip.current = false; return }
    if (follow && el) setPinned(el.scrollHeight - el.scrollTop - el.clientHeight < 40)
  }
  useLayoutEffect(() => {
    const el = scroller.current
    if (!el || (follow && !pinned)) return
    const box = extraBox.current
    // a long briefing that is the last thing is shown from its heading; everything else: the newest message at the bottom
    const was = el.scrollTop
    if (follow && box && extra?.alignTop && !after.length) el.scrollTop += box.getBoundingClientRect().top - el.getBoundingClientRect().top - 8
    else el.scrollTop = el.scrollHeight
    skip.current = el.scrollTop !== was
  }, [items.length, last?.text, last?.pending, !!extra, after.length, pinned, follow])
  const jump = () => { const el = scroller.current; if (el) { el.scrollTop = el.scrollHeight; setPinned(true) } }
  const nothing = !items.length && !extra
  return (
    <>
      <div className="con-body sc" ref={scroller} onScroll={onScroll} data-testid="transcript">
        {follow && !nothing && <p className="con-order small">舊訊息在上 ↓ 新訊息在下</p>}
        {nothing && (guide ?? <p className="muted con-empty">{empty}</p>)}
        {before.map((b) => <BubbleRow key={b.key} b={b} />)}
        {extra && <div ref={extraBox}>{extra.node}</div>}
        {after.map((b) => <BubbleRow key={b.key} b={b} />)}
      </div>
      {follow && !pinned && <button className="con-jump" onClick={jump} data-testid="con-jump">↓ 跳到最新</button>}
    </>
  )
}

/** A long reply: the conclusion first, 「展開詳細」 shows the whole original text (unchanged). */
export function leadOf(text: string): { lead: string; rest: boolean } {
  const parts = text.split(/(?<=[。！？!?；;\n])/).map((x) => x.trim()).filter(Boolean)
  let lead = ''
  for (const sent of parts) {
    if (lead && Array.from(lead + sent).length > 90) break
    lead += sent
    if (Array.from(lead).length >= 24) break
  }
  lead = lead.replace(/[；;\s]+$/, '')
  return { lead, rest: lead.length < text.trim().length }
}

function ReplyText({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLSpanElement>(null)
  // keep the opened reply readable: bring it into view inside the chat scroller
  useEffect(() => { if (open) box.current?.scrollIntoView({ block: 'nearest' }) }, [open])
  const { lead, rest } = leadOf(text)
  if (!rest || Array.from(text).length <= 90) return <>{text}</>
  return (
    <>
      <span ref={box} data-testid="reply-text">{open ? text : lead}</span>
      <button type="button" className="sc-more" aria-expanded={open} onClick={() => setOpen(!open)} data-testid="reply-more">{open ? '收合 ▴' : '展開詳細 ▾'}</button>
    </>
  )
}

function BubbleRow({ b }: { b: Bubble }) {
  const [open, setOpen] = useState(false)
  const steps = b.steps ?? []
  return (
    <div className={`sc-row ${b.who}`} data-testid={b.who === 'me' ? 'sc-me' : 'sc-agent'}>
      <div className={`sc-b ${b.pending ? 'pending' : ''}`}>
        {b.who === 'agent' && !b.pending && <b className="sc-ag">NemoClaw</b>}
        {b.pending && b.who === 'agent' ? <><i className="spin" />NemoClaw 思考中…</> : b.who === 'agent' ? <ReplyText text={plain(b.text)} /> : plain(b.text)}
        {!b.pending && <time className="sc-time">{fmtHM(b.ts)}</time>}
      </div>
      {steps.length > 0 && (
        <button type="button" className="sc-tools" onClick={() => setOpen((o) => !o)} aria-expanded={open} data-testid="sc-tools">
          {steps.map((s) => s.label).join('・')}
        </button>
      )}
      {open && <ul className="sc-tool-list">{steps.map((s, i) => <li key={i} className={s.ok ? '' : 'bad'}>{s.label}：{plain(s.summary)}</li>)}</ul>}
      {b.fellBack && <p className="sc-fb" title={b.error ?? ''} data-testid="sc-fallback">模型沒有回應，這句先用規則處理</p>}
    </div>
  )
}

/** One subagent in the 主控台 panel: where it is, what it did (tools with results), the workers, 中斷, and what the
 *  supervisor said about its waiting plan (`talk`). */
export function SubProgress({ ev, talk = [] }: { ev: Event; talk?: Bubble[] }) {
  const { state, transport } = useWarRoom()
  const sa = state.subagents?.items.find((x) => x.id === ev.id)
  const { items } = useTranscript(ev)
  const tools = items.filter((i): i is ToolItem => i.k === 'tool')
  const tasks = (state.world?.tasks?.items ?? []).filter((t) => t.event_id === ev.id)
  const open = ev.state !== 'closed' && ev.state !== 'safe_stop'
  const [busy, setBusy] = useState(false)
  const stop = async () => {
    if (!transport?.interrupt || !window.confirm('中斷 NemoClaw 處理這件事？不會再呼叫工具，待核准的卡作廢、還沒做完的人員任務取消，排程不變。')) return
    setBusy(true)
    try { await transport.interrupt(ev.id) } catch { /* the status shows what happened */ } finally { setBusy(false) }
  }
  return (
    <div className="con-body sp" data-testid="sub-progress">
      <p className="sp-status"><b>{sa ? plainStatus(sa) : STATE_LABEL[ev.state]}</b>
        <span className="muted small">{STATE_LABEL[ev.state]}・第 {(ev.prior_rounds?.length ?? 0) + 1} 輪・工具 {tools.length}{tasks.length ? `・現場 ${tasks.filter((t) => t.status === '完成').length}/${tasks.length}` : ''}</span>
        {open && transport?.interrupt && <button className="btn-ghost danger sp-stop" disabled={busy} onClick={() => void stop()} data-testid="sp-interrupt">中斷</button>}
      </p>
      {ev.outcome && <p className="small">{plain(ev.outcome)}</p>}
      <ul className="sp-tools" aria-label="NemoClaw 用過的工具">
        {tools.map((t) => {
          let line = ''
          if (t.status === 'ok' && t.data) { try { line = resultText(t.tool, t.args, t.data) } catch { line = '' } }
          return <li key={t.key} className={t.status}><span className="muted">{fmtHM(t.ts)}</span> {TOOL_LABEL[t.tool] ?? t.tool}：{t.status === 'running' ? '執行中…' : t.status === 'failed' ? `失敗 ${t.error ?? ''}` : plain(line) || '完成'}</li>
        })}
        {!tools.length && <li className="muted">還沒用工具</li>}
      </ul>
      {tasks.length > 0 && <TaskList tasks={tasks} eventId={ev.id} compact testId="sp-tasks" />}
      {talk.length > 0 && <div className="sc sp-talk" data-testid="sp-talk">{talk.map((b) => <BubbleRow key={b.key} b={b} />)}</div>}
    </div>
  )
}
