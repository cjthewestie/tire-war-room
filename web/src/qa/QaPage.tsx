// QA 模式 (#qa): helps answer the finals judges' technical questions about the agent framework (CJ, 2026-10-22:
// 「QA mode是評審遇到技術框架問題的時候可以幫我回答的」). Server: POST /api/qa/ask (warroom/agent/qa.py) answers only from
// the framework knowledge base, read-only and offline; every answer lists its 證據 (file:line), each opening in the
// viewer (GET /api/qa/doc) or on the live page that shows it. 大字模式 is for the projector.
import { useEffect, useRef, useState } from 'react'
import { apiGet, supervisorPost } from '../transport'
import './qa.css'

interface Evidence { ref: string; path: string; line: number | null; title: string; kind: string; live: { href: string; label: string } | null }
interface QaAnswer {
  id: string; question: string; answer: string; evidence: Evidence[]; by: string; gate: string | null
  problems: string[]; error: string | null; ms: number
}

/** The 12 most likely judge questions: the chip label, and the question sent (a little more explicit). */
const QA_CHIPS: { label: string; q: string }[] = [
  { label: '為什麼不用 NAT 做狀態機', q: '為什麼不用 NAT 做狀態機？' },
  { label: 'AI 會不會亂改產線', q: 'AI 會不會亂改產線？' },
  { label: '怎麼防 prompt injection', q: '怎麼防 prompt injection？' },
  { label: '只有哪裡會叫 AI', q: '狀態機裡只有哪裡會呼叫 AI（LLM）？' },
  { label: '斷網還能跑嗎', q: '斷網還能跑嗎？' },
  { label: '跟 OpenClaw 差在哪', q: '跟 OpenClaw、Hermes、Claude Code 這類 agent 差在哪？' },
  { label: '安全核心是什麼', q: '安全核心是什麼？' },
  { label: '模型多大跑在哪', q: '用的是什麼模型？模型多大、跑在哪裡？' },
  { label: '為什麼工具要模組化', q: '為什麼工具要模組化、做成固定的白名單？' },
  { label: 'context 怎麼控制', q: 'context 太長怎麼控制？' },
  { label: '怎麼確認 AI 計劃有效', q: '怎麼確認 AI 的計劃有效、是對的？' },
  { label: '怎麼導入新工廠', q: '換一間新工廠要怎麼導入？' },
]

const BY_LABEL: Record<string, string> = {
  model: 'Nemotron 依文件回答', extract: '文件原文（模型回答沒通過檢查或模型離線）', fallback: '文件裡沒有',
  model_no_answer: '文件裡沒有', safety_filter: '安全過濾擋下',
}
const BIG_KEY = 'warroom.qa.big'

function readBig(): boolean {
  try { return localStorage.getItem(BIG_KEY) === '1' } catch { return false }
}

function conversationId(): string {
  return `qa-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`
}

function DocViewer({ ev, onClose }: { ev: Evidence; onClose: () => void }) {
  const [text, setText] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const hit = useRef<HTMLDivElement>(null)
  useEffect(() => {
    apiGet<{ text: string }>(`/api/qa/doc?path=${encodeURIComponent(ev.path)}`).then((d) => setText(d.text)).catch((e) => setErr(String(e)))
  }, [ev.path])
  useEffect(() => { hit.current?.scrollIntoView({ block: 'center' }) }, [text])
  useEffect(() => {
    const f = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', f)
    return () => window.removeEventListener('keydown', f)
  }, [onClose])
  const lines = text?.split('\n') ?? []
  const at = ev.line ?? 0
  return (
    <div className="qa-viewer-scrim" onClick={onClose}>
      <div className="qa-viewer" role="dialog" aria-modal="true" aria-label={`文件 ${ev.ref}`} onClick={(e) => e.stopPropagation()} data-testid="qa-viewer">
        <div className="qa-viewer-h">
          <b>{ev.path}</b>{ev.line != null && <span>第 {ev.line} 行</span>}
          {ev.live && <a href={ev.live.href} className="qa-live">在「{ev.live.label}」看 →</a>}
          <button className="x" onClick={onClose} aria-label="關閉文件">✕</button>
        </div>
        {err ? <p className="qa-err">打不開這份文件：{err}</p> : text == null ? <p className="qa-dim">載入中…</p> : (
          <div className="qa-code">
            {lines.map((ln, i) => {
              const n = i + 1
              const on = at > 0 && n >= at && n < at + 4
              return <div key={n} ref={n === at ? hit : undefined} className={on ? 'on' : ''}><i>{n}</i><span>{ln || ' '}</span></div>
            })}
          </div>
        )}
      </div>
    </div>
  )
}

export function QaPage() {
  const [q, setQ] = useState('')
  const [big, setBig] = useState(readBig)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [answers, setAnswers] = useState<QaAnswer[]>([])
  const [view, setView] = useState<Evidence | null>(null)
  const conv = useRef(conversationId())
  const input = useRef<HTMLTextAreaElement>(null)

  const toggleBig = () => setBig((b) => { try { localStorage.setItem(BIG_KEY, b ? '0' : '1') } catch { /* private mode */ } return !b })

  const ask = async (question: string) => {
    const text = question.trim()
    if (!text || busy) return
    setBusy(true); setErr(null); setQ(text)
    try {
      const a = await supervisorPost<QaAnswer>('/api/qa/ask', { question: text, conversation_id: conv.current })
      setAnswers((xs) => [a, ...xs].slice(0, 20))
    } catch (e) {
      setErr(`沒有拿到回答：${String(e instanceof Error ? e.message : e)}`)
    } finally {
      setBusy(false)
    }
  }

  const [cur, ...past] = answers
  return (
    <section className={`qa-page ${big ? 'qa-big' : ''}`} data-testid="qa-page" aria-label="QA 模式">
      <div className="qa-head">
        <div>
          <h2>QA 模式・評審技術問題</h2>
          <p className="qa-dim">只根據專案文件回答，附出處；本機 Nemotron，離線可用。文件裡沒有的，會直接說「現場由 CJ 補充」。</p>
        </div>
        <button className={`qa-big-btn ${big ? 'on' : ''}`} aria-pressed={big} onClick={toggleBig} data-testid="qa-big-toggle">大字模式</button>
      </div>

      <form className="qa-ask" onSubmit={(e) => { e.preventDefault(); void ask(q) }}>
        <label htmlFor="qa-q" className="sr-only">評審的問題</label>
        <textarea id="qa-q" ref={input} value={q} rows={2} maxLength={300} placeholder="輸入評審的問題，例如：AI 會不會亂改產線？"
          onChange={(e) => setQ(e.target.value)} data-testid="qa-input"
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void ask(q) } }} />
        <button type="submit" className="qa-go" disabled={busy || !q.trim()} data-testid="qa-ask">{busy ? '回答中…' : '問'}</button>
      </form>

      <div className="qa-chips" role="list" aria-label="常見問題">
        {QA_CHIPS.map((c) => (
          <button key={c.label} role="listitem" className="qa-chip" disabled={busy} onClick={() => void ask(c.q)} data-testid="qa-chip">{c.label}</button>
        ))}
      </div>

      {err && <p className="qa-err" role="alert">{err}</p>}
      {busy && !cur && <div className="qa-card qa-dim" aria-live="polite">本機模型回答中，通常 2–15 秒…</div>}
      {cur && (
        <article className={`qa-card ${cur.by === 'model' || cur.by === 'extract' ? '' : 'none'}`} data-testid="qa-answer" aria-live="polite">
          <p className="qa-q">問：{cur.question}</p>
          <p className="qa-a">{cur.answer}</p>
          <p className="qa-meta">{BY_LABEL[cur.by] ?? cur.by}・{(cur.ms / 1000).toFixed(1)} 秒</p>
          {cur.evidence.length > 0 && (
            <div className="qa-ev" data-testid="qa-evidence">
              <h3>證據</h3>
              <ul>
                {cur.evidence.map((e) => (
                  <li key={e.ref}>
                    <button className="qa-ref" onClick={() => setView(e)} title={e.title}>{e.ref}</button>
                    <span className="qa-ev-t">{e.kind === 'code' ? '' : e.title}</span>
                    {e.live && <a className="qa-live" href={e.live.href}>{e.live.label} →</a>}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </article>
      )}
      {past.length > 0 && (
        <details className="qa-past">
          <summary>這場問過的 {past.length} 題</summary>
          <ul>{past.map((a) => <li key={a.id}><button onClick={() => setAnswers((xs) => [a, ...xs.filter((x) => x.id !== a.id)])}>{a.question}</button></li>)}</ul>
        </details>
      )}
      {view && <DocViewer ev={view} onClose={() => setView(null)} />}
    </section>
  )
}
