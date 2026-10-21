// Home 主控台: the 核准區 list, the plan of the selected item (核准並派發 / 討論), and the conversation with the 主 agent
// (issue #54; CJ, 2026-10-20: 「單純一點好嗎」): plain bubbles and one input (CJ, 2026-10-21: the subagent switcher is gone).
// Every step behind it is on the Agent page (#console), linked from 「看完整過程」.
import { useEffect, useMemo, useState } from 'react'
import type { ApprovalRequest, Event, Line } from '../contracts'
import { ms } from '../format'
import { useWarRoom } from '../useWarRoom'
import { ApprovalList } from './ApprovalList'
import { HomeOverview } from './HomeOverview'
import { HomeStatus } from './HomeStatus'
import { useHomeSelection } from './homeSelection'
import { PlanArea } from './PlanArea'
import { isDialogue, type Item, type LocalItem } from './transcript'
import { PendingNotice } from './PendingNotice'
import { setReplan, useReplans } from './replanStore'
import { ChatBar, type BarFocus, type BarMode } from './ChatBar'
import { Bubbles } from './SimpleChat'
import { useMainThread } from './mainThread'
import { useTranscript } from './useTranscript'
import { BriefingView } from '../components/BriefingView'
import { useEventBriefing } from '../useBriefing'
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
  const { events, list, sel, ev } = useHomeSelection(picked)
  const { items } = useTranscript(ev)
  const [focus, setFocus] = useState<BarFocus | null>(null)
  const chat = (mode?: BarMode, text?: string, send?: boolean) => { setFocus((f) => ({ mode, text, send, n: (f?.n ?? 0) + 1 })) }
  // 「請 AI 重排」: when the task's approval card arrives, select it so the plan area shows 核准 / 討論
  const replans = useReplans()
  const arrived = Object.values(replans).find((r) => r.state === 'planning' && r.eventId && state.pending.some((p) => p.event_id === r.eventId))
  // switch only while the supervisor is still looking at that order (never pull the screen away from something else)
  const looking = sel && picked === sel.key ? sel.orderId : null        // the supervisor opened it (not the default pick)
  useEffect(() => {
    if (!arrived?.eventId) return
    setReplan(arrived.orderId, { state: 'ready' })
    if (looking === arrived.orderId) setPicked(arrived.eventId)
  }, [arrived?.eventId, arrived?.orderId, looking, setPicked])
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
      <ApprovalList items={list} selected={sel?.key ?? null} onPick={setPicked} />
      <PlanArea sel={sel} ev={ev} items={items} opened={!!sel && picked === sel.key} onDiscuss={() => chat('discuss')} onAsk={() => chat('ask')} onCommand={(t) => chat('command', t)} />
      <Console planEv={ev} items={items} asks={asks} setAsks={setAsks} focus={focus} onFull={onFull} onGuide={(t) => chat('ask', t, true)} />
    </div>
    </>
  )
}

/** The newest pending card of an event (a v2 replaces its v1). */
function latestReq(pending: ApprovalRequest[], evId: string | undefined): ApprovalRequest | null {
  if (!evId) return null
  return [...pending].filter((p) => p.event_id === evId).sort((a, b) => (a.created_at < b.created_at ? 1 : -1))[0] ?? null
}

const GUIDE_QUESTIONS = ['今天有哪些訂單？', '哪些訂單有交期風險？', '7 號機現在怎樣？', '如果硫化機停機，會影響哪些訂單？']

/** The 主控台 conversation panel: the one NemoClaw (主 agent) thread (CJ, 2026-10-21: 「我不要長這樣」 about the
 *  「主 agent ▸ 派出的子 agent」 switcher, so it is removed; issue #100: the 此事項／全部 switcher is gone too). The plan on
 *  screen sits in the thread as a short 待核准 notice (or the briefing of its state), placed by its own time. Each
 *  subagent's progress is on the Agent page, via 看完整過程. */
function Console({ planEv, items, asks, setAsks, focus, onFull, onGuide }: {
  planEv: Event | null; items: Item[]
  asks: LocalItem[]; setAsks: React.Dispatch<React.SetStateAction<LocalItem[]>>; focus: BarFocus | null; onFull?: (eventId?: string) => void
  onGuide: (text: string) => void
}) {
  const { state, transport, connected } = useWarRoom()
  const thread = useMainThread(asks)
  // the input talks about the plan on screen
  const talkEv = planEv
  const waiting = !!talkEv && talkEv.state === 'await_approval' && state.pending.some((p) => p.event_id === talkEv.id)
  const full = (e: React.MouseEvent) => { if (onFull) { e.preventDefault(); onFull(talkEv?.id) } }
  // 主管摘要 (issue #93): one briefing for the plan on screen, following its state (待核准 / 執行中 / 處置完成)
  const brief = useEventBriefing(talkEv)
  const req = useMemo(() => latestReq(state.pending, talkEv?.id), [state.pending, talkEv?.id])
  // the briefing sits among the messages by its own time: a 待核准 notice by its card's created_at (so a v2 comes after the
  // 討論 that asked for it), any other state by the latest step of the record. No time → it stays last.
  const briefAt = useMemo(() => {
    if (!brief) return NaN
    if (brief.state === 'pending' && req) return ms(req.created_at)
    const stepTs = items.filter((i) => !isDialogue(i)).map((i) => ms(i.ts)).filter((t) => !isNaN(t))
    return stepTs.length ? Math.max(...stepTs) : NaN
  }, [brief, req, items])
  const toPlan = () => (document.querySelector('[data-testid=plan-area] [data-testid=approval-visual]') ?? document.querySelector('[data-testid=plan-area]'))?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  const extra = talkEv && brief ? {
    at: briefAt, alignTop: !(brief.state === 'pending' && req),
    node: brief.state === 'pending' && req
      ? <PendingNotice ev={talkEv} req={req} onPlan={toPlan} />
      : (
        <div className="con-brief" data-testid="con-brief">
          <BriefingView b={brief} footer={(
            <a className="br-link" href={`#console?event=${encodeURIComponent(talkEv.id)}`} onClick={full} data-testid="see-record">查看處理紀錄</a>
          )} />
        </div>
      ),
  } : null
  const askReady = !!transport?.message && (transport.mode !== 'live' || connected)
  const guide = (
    <div className="con-guide" data-testid="con-guide">
      <h3>我是 NemoClaw</h3>
      <p className="muted small">戰情室的主 agent。我可以幫你：</p>
      <ul><li>查今日訂單與交期風險</li><li>看機台狀態與溫度</li><li>評估停機對訂單的影響</li></ul>
      <p className="muted small">試試這些問題：</p>
      {GUIDE_QUESTIONS.map((q) => <button key={q} type="button" className="con-guide-q" disabled={!askReady} onClick={() => onGuide(q)}>{q}</button>)}
      {!askReady && <p className="con-offline" role="status" data-testid="guide-offline">目前未連線後端，無法使用 NemoClaw 問答</p>}
    </div>
  )

  return (
    <section className="console panel" aria-label="NemoClaw 對話">
      <header className="con-h">
        <div className="con-title">
          <h2>NemoClaw 對話</h2>
          <a className="con-full" href={`#console${talkEv ? `?event=${encodeURIComponent(talkEv.id)}` : ''}`} onClick={full} data-testid="see-full">看完整過程 →</a>
        </div>
      </header>
      <Bubbles items={thread} empty="跟 NemoClaw 說要做的事，或問工廠現在的狀況。" extra={extra} guide={guide} follow />
      <ChatBar ev={talkEv} waiting={waiting} setAsks={setAsks} focus={focus} />
    </section>
  )
}
