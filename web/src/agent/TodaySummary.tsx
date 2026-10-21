// 總覽 今日總結 (CJ, 2026-10-22: 「這邊應該叫做總覽為什麼不能像claude mem 有前面session總結 應該要有個今日總結」): the claude-mem
// previous-session card on top of the 總覽 chat. Today's orders in, events handled (結案 / 安全停止 / 進行中), approvals and
// the key observations, live from the observations until the day's summary exists (GET /api/memory/today); the previous
// day's summary folds open as 前一日總結. Read-only; refreshed every 10 s.
import { useEffect, useState } from 'react'
import { fmtHM } from '../format'
import { plain } from '../plain'
import { apiGet } from '../transport'
import { useWarRoom } from '../useWarRoom'
import { ObChips } from '../admin/MemoryTimeline'

interface Card { name: string; label: string; sentences: string[]; source: string | null; handled?: string; learned?: string; completed?: string; todo?: string }
interface Today {
  label: string; source: 'summary' | 'live'; observations: number; orders_in: number; rush: number; shipped: number
  events: { closed: number; safe_stop: number; open: number; cancelled?: number }; approvals: { approve: number; reject: number; sent: number }
  key: { id: string; ts: string; type: string; title: string; icon: string }[]; summary: Card | null; previous: Card | null
}

function CardRows({ c }: { c: Card }) {
  return (
    <>
      {(c.handled || c.learned || c.completed || c.todo) && (
        <dl className="ts-g">
          {c.handled && <><dt>處理了</dt><dd>{c.handled}</dd></>}
          {c.learned && <><dt>學到</dt><dd>{c.learned}</dd></>}
          {c.completed && <><dt>完成</dt><dd>{c.completed}</dd></>}
          {c.todo && <><dt>待辦</dt><dd>{c.todo}</dd></>}
        </dl>
      )}
      {!!c.sentences.length && <p className="ts-s">{c.sentences.join('')}</p>}
    </>
  )
}

export function TodaySummary() {
  const { transport, connected } = useWarRoom()
  const [t, setT] = useState<Today | null>(null)
  useEffect(() => {
    if (transport?.mode !== 'live' || !connected) return
    let off = false
    const load = () => apiGet<Today>('/api/memory/today').then((d) => { if (!off) setT(d) }).catch(() => {})
    void load()
    const id = setInterval(load, 10_000)
    return () => { off = true; clearInterval(id) }
  }, [transport, connected])
  if (!t) return null
  const ev = t.events
  return (
    <section className="ts" aria-label="今日總結" data-testid="today-summary">
      <p className="ts-k"><b>🗓 今日總結</b><span className="muted small">{t.label}・{t.source === 'summary' ? '每日摘要' : `即時，依今天 ${t.observations} 筆紀錄`}</span></p>
      <dl className="ts-g">
        <dt>訂單</dt><dd>進來 {t.orders_in} 張{t.rush ? `（急單 ${t.rush}）` : ''}・出貨 {t.shipped} 次</dd>
        <dt>事件</dt><dd>結案 {ev.closed}・安全停止 {ev.safe_stop}・進行中 {ev.open}{ev.cancelled ? `・取消 ${ev.cancelled}` : ''}</dd>
        <dt>核准</dt><dd>送核准 {t.approvals.sent}・核准 {t.approvals.approve}・否決 {t.approvals.reject}</dd>
        {!!t.key.length && <><dt>重點</dt><dd><ul className="ts-key">{t.key.map((o) => <li key={o.id}><span aria-hidden>{o.icon}</span> <time>{fmtHM(o.ts)}</time> {plain(o.title)}</li>)}</ul></dd></>}
      </dl>
      {!!t.key.length && <ObChips ids={t.key.map((o) => o.id)} label="看細節" />}
      {t.summary && <CardRows c={t.summary} />}
      {t.previous && (
        <details className="ts-prev" data-testid="prev-summary">
          <summary>前一日總結・{t.previous.label}</summary>
          <CardRows c={t.previous} />
        </details>
      )}
    </section>
  )
}
