// The input bar shared by the 主控台 Agent panel and the Agent page (issue #10, #52): three modes — 討論這個計劃
// (POST /api/events/{id}/discuss), 下新任務 (POST /api/command), 只查不改 (POST /api/ask) — bound to the event the
// page has selected. Messages being sent and 問戰情室 answers go into the one App-level list (`asks`), and the server's
// audit rows replace them, so a message sent on one page shows on the other.
import { useEffect, useRef, useState } from 'react'
import type { AskAnswer, CommandReply, DiscussReply, Event } from '../contracts'
import { machineLabel } from '../factory'
import { useWarRoom } from '../useWarRoom'
import { newConversationId } from '../components/ordersToday'
import type { ChatItem, LocalItem } from './transcript'

const ASK_EXAMPLES = ['今天有哪些訂單？', '哪些訂單有遲交風險？', '7 號機現在怎樣？']

export interface BarFocus { mode?: BarMode; text?: string; n: number }

let askSeq = 0

export type BarMode = 'discuss' | 'command' | 'ask'
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

export function ChatBar({ ev, waiting, setAsks, onPick, focus }: {
  ev: Event | null; waiting: boolean; setAsks: React.Dispatch<React.SetStateAction<LocalItem[]>>; onPick: (id: string) => void
  focus: BarFocus | null
}) {
  const { transport, state } = useWarRoom()
  const [q, setQ] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [picked, setPicked] = useState<BarMode | null>(null)
  const input = useRef<HTMLTextAreaElement>(null)
  // one conversation per page load: the server keeps order ids only, so 「那張單呢？」 works and a reload starts fresh
  const convId = useRef(newConversationId())
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
        const a: AskAnswer = await transport.ask(text, convId.current)
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
