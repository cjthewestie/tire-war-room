// One input, one 主 agent (issue #54; CJ, 2026-10-20: 「agent 回答很奇怪，為什麼要只查不改，他應該就是個 orchestrator」).
// No mode switch: the 主 agent classifies each message itself (POST /api/main/message) — small talk and "who are you"
// get a direct reply with no tool, a question gets a read-only 查詢 answer, a change to the selected waiting plan
// becomes a discussion / replan, a task becomes a plan proposal, and unclear text gets one clarifying question.
// Used by the 主控台 panel and the Agent page, bound to the event the page has selected. Messages being sent and the
// answers go into the one App-level list (`asks`); the server's audit rows replace them, so both pages show them.
import { useEffect, useRef, useState } from 'react'
import type { Event, MainReply } from '../contracts'
import { useWarRoom } from '../useWarRoom'
import type { ChatItem, LocalItem } from './transcript'

/** `mode` is kept for callers (討論 / 問 AI buttons) and ignored: the 主 agent decides. */
export interface BarFocus { mode?: BarMode; text?: string; n: number }
export type BarMode = 'discuss' | 'command' | 'ask'

let askSeq = 0
const nowIso = () => new Date().toISOString()
const EX_WAITING = ['9 號機今晚要保養', '不要跨組調人', '為什麼用 9 號機？']
const EX_FREE = ['7 號機現在怎樣？', '#M-201 可能晚交，請重排', '妳是誰']

export function ChatBar({ ev, waiting, setAsks, onPick, focus }: {
  ev: Event | null; waiting: boolean; setAsks: React.Dispatch<React.SetStateAction<LocalItem[]>>; onPick: (id: string) => void
  focus: BarFocus | null
}) {
  const { transport, state } = useWarRoom()
  const [q, setQ] = useState('')
  const [busy, setBusy] = useState(false)
  const input = useRef<HTMLTextAreaElement>(null)
  const [seen, setSeen] = useState(0)
  if (focus && focus.n !== seen) {
    setSeen(focus.n)
    if (focus.text) setQ(focus.text)
  }
  useEffect(() => { if (focus?.n) setTimeout(() => input.current?.focus(), 60) }, [focus?.n])
  const can = !!transport?.message
  const lanDown = state.snapshot?.header.network === 'lan_down'
  const ft = state.clock?.factory_time ?? state.snapshot?.header.factory_time ?? ''
  const chat = (role: ChatItem['role'], kind: ChatItem['kind'], text: string, key: string, n: number, ts: string, pending: boolean): ChatItem => ({
    k: 'chat', key, ts, seq: 1e9 + n, role, kind, text, by: role === 'me' ? 'supervisor' : 'main_agent', intent: null, version: null,
    changes: [], constraints: [], classifiedBy: null, fellBack: false, pending,
  })
  const send = async (raw: string) => {
    const text = raw.trim()
    if (!text || busy || !can || !transport?.message) return
    const n = ++askSeq
    const keys = [`me-${n}`, `ag-${n}`]
    const ts = ft || nowIso()
    setQ('')
    setBusy(true)
    setAsks((xs) => [...xs, chat('me', 'command', text, keys[0], n, ts, true), chat('agent', 'reply', '主 agent 判讀中…', keys[1], n + 0.5, ts, true)])
    const drop = () => setAsks((xs) => xs.filter((x) => !keys.includes(x.key)))
    try {
      const r: MainReply = await transport.message(text, waiting ? ev?.id ?? null : null)
      if (r.intent === 'question' && r.ask) {
        const a = r.ask
        setAsks((xs) => [...xs.filter((x) => !keys.includes(x.key)),
          { k: 'ask', key: `ask-${n}`, ts: a.factory_time || ts, seq: 1e9 + n, question: text, answer: a, error: null }])
      } else if (r.intent === 'change') {
        drop()                                   // the discussion's audit rows carry it (and the v2 card)
      } else if (r.intent === 'task' && r.event_id) {
        drop()
        onPick(r.event_id)
      } else {
        setAsks((xs) => xs.map((x) => (x.key === keys[0] && x.k === 'chat' ? { ...x, pending: false }
          : x.key === keys[1] && x.k === 'chat' ? { ...x, pending: false, text: r.reply ?? '' , classifiedBy: r.by } : x)))
      }
    } catch (e) {
      setAsks((xs) => xs.map((x) => (x.key === keys[1] && x.k === 'chat' ? { ...x, pending: false, text: `送出失敗：${String(e)}` } : x)))
    } finally { setBusy(false) }
  }
  return (
    <form className="askbar" onSubmit={(e) => { e.preventDefault(); void send(q) }} data-mode="main">
      <div className="askbar-modes">
        <span className="askbar-hint">跟主 agent 說：要做的事、要改的計劃{waiting ? '（目前選的待核准計劃）' : ''}，或想問的狀況</span>
      </div>
      <div className="askbar-ex">
        {(waiting ? EX_WAITING : EX_FREE).map((x) => <button type="button" key={x} disabled={!can || busy} onClick={() => void send(x)}>{x}</button>)}
      </div>
      <div className="askbar-in">
        <textarea ref={input} rows={1} maxLength={300} value={q} disabled={!can || busy} aria-label="跟主 agent 說"
          data-testid="chat-input"
          placeholder={can ? (lanDown ? 'LAN 中斷時請在現場觸控螢幕操作' : '例如：#M-201 可能晚交，請重排・7 號機現在怎樣？（Enter 送出）') : '模擬資料模式不支援，需連線後端'}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(q) } }} />
        <button type="submit" className="btn-send" disabled={!can || busy || !q.trim()} data-testid="chat-send">{busy ? '判讀中…' : '送出'}</button>
      </div>
    </form>
  )
}
