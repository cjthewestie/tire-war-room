// One input, one 主 agent (issue #54; CJ, 2026-10-20: 「agent 回答很奇怪，為什麼要只查不改，他應該就是個 orchestrator」).
// No mode switch: the 主 agent classifies each message itself (POST /api/main/message) — small talk and "who are you"
// get a direct reply with no tool, a question gets a read-only 查詢 answer, a change to the selected waiting plan
// becomes a discussion / replan, a task becomes a plan proposal, and unclear text gets one clarifying question.
// Used by the 主控台 panel and the Agent page, bound to the event the page has selected. Messages being sent and the
// answers go into the one App-level list (`asks`); the server's audit rows replace them, so both pages show them.
import { useEffect, useRef, useState } from 'react'
import type { ChatMode, Event, MainReply } from '../contracts'
import { useWarRoom } from '../useWarRoom'
import type { ChatItem, LocalItem } from './transcript'

/** `mode` is kept for callers (討論 / 問 AI buttons) and ignored: the 主 agent decides. */
export interface BarFocus { mode?: BarMode; text?: string; n: number; /** send `text` at once (the guide questions in an empty chat) instead of only filling the box */ send?: boolean }
export type BarMode = 'discuss' | 'command' | 'ask'

let askSeq = 0
const nowIso = () => new Date().toISOString()

export function ChatBar({ ev, session = null, waiting, setAsks, focus, onSent, onReply, mode = 'task' }: {
  ev: Event | null; waiting: boolean; setAsks: React.Dispatch<React.SetStateAction<LocalItem[]>>
  /** issue #189: the session of a card with no event (order:<id>): sent as the message's task so it has its own context */
  session?: string | null
  focus: BarFocus | null; onSent?: (about: string | null) => void
  /** the reply arrived (a 下新任務 reply carries the new task's event id: the 主控台 opens its session) */
  onReply?: (r: MainReply) => void
  /** 'overview': the Agent page's 總覽 conversation (no task selected, its own memory) */
  mode?: ChatMode
}) {
  const { transport, state } = useWarRoom()
  const [q, setQ] = useState('')
  const [busy, setBusy] = useState(false)
  const input = useRef<HTMLTextAreaElement>(null)
  const [seen, setSeen] = useState(0)
  if (focus && focus.n !== seen) {
    setSeen(focus.n)
    if (focus.text && !focus.send) setQ(focus.text)
  }
  // 討論: the caret goes into this input, scrolled into view (on a narrow screen the chat sits below the card, and
  // 「按了討論沒有用」 was the click focusing an input off screen). #101's fix, lost when #112 merged.
  useEffect(() => {
    if (!focus?.n) return
    setTimeout(() => { input.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); input.current?.focus() }, 60)
  }, [focus?.n])
  // the guide questions of an empty chat go out at once
  const sentFocus = useRef(0)
  useEffect(() => {
    if (focus?.send && focus.text && focus.n !== sentFocus.current) { sentFocus.current = focus.n; void send(focus.text) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus?.n])
  const can = !!transport?.message
  const lanDown = state.snapshot?.header.network === 'lan_down'
  const ft = state.clock?.factory_time ?? state.snapshot?.header.factory_time ?? ''
  const chat = (role: ChatItem['role'], kind: ChatItem['kind'], text: string, key: string, n: number, ts: string, pending: boolean, about: string | null): ChatItem => ({
    k: 'chat', key, ts, seq: 1e9 + n, role, kind, text, by: role === 'me' ? 'supervisor' : 'main_agent', intent: null, version: null,
    changes: [], constraints: [], classifiedBy: null, fellBack: false, pending, about, mode, selected: mode === 'overview' ? null : ev?.id ?? session ?? about,
  })
  const send = async (raw: string) => {
    const text = raw.trim()
    if (!text || busy || !can || !transport?.message) return
    const n = ++askSeq
    const about = waiting ? ev?.id ?? null : null
    onSent?.(about)
    const keys = [`me-${n}`, `ag-${n}`]
    const ts = ft || nowIso()
    setQ('')
    setBusy(true)
    setAsks((xs) => [...xs, chat('me', 'command', text, keys[0], n, ts, true, about), chat('agent', 'reply', 'Agent Nemo 思考中…', keys[1], n + 0.5, ts, true, about)])
    try {
      // the selected task rides along even when it has no waiting card: the chat talks about it (CJ 2026-10-22: 「A」)
      const r: MainReply = await transport.message(text, ev?.id ?? session ?? about, mode)
      // the reply (with its tool line) stays here until the server's audit row of this turn replaces it
      setAsks((xs) => xs.map((x) => (x.key === keys[0] && x.k === 'chat' ? { ...x, pending: false, turn: r.id }
        : x.key === keys[1] && x.k === 'chat' ? { ...x, pending: false, text: r.reply, turn: r.id, steps: r.steps, fellBack: r.fell_back, error: r.error, links: r.links } : x)))
      onReply?.(r)
    } catch (e) {
      setAsks((xs) => xs.map((x) => (x.key === keys[1] && x.k === 'chat' ? { ...x, pending: false, text: `送出失敗：${String(e)}` } : x)))
    } finally { setBusy(false) }
  }
  return (
    <form className="askbar" onSubmit={(e) => { e.preventDefault(); void send(q) }} data-mode="main">
      <div className="askbar-in">
        <textarea ref={input} rows={1} maxLength={300} value={q} disabled={!can || busy} aria-label="跟 Agent Nemo 說"
          data-testid="chat-input"
          data-plan={waiting ? 'waiting' : undefined}
          placeholder={can ? (lanDown ? 'LAN 中斷時請在現場觸控螢幕操作' : waiting ? '這個計劃要怎麼改，直接說' : mode === 'overview' ? '問全部任務、過去紀錄或工廠的事…' : '跟 Agent Nemo 說…') : '模擬資料模式不支援，需連線後端'}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(q) } }} />
        <button type="submit" className="btn-send" disabled={!can || busy || !q.trim()} data-testid="chat-send">{busy ? '判讀中…' : '送出'}</button>
      </div>
    </form>
  )
}
