// Home 主控台: the 處理清單 list, the plan of the selected item (核准 / 討論), and the conversation with the 主 agent
// (issue #54; CJ, 2026-10-20: 「單純一點好嗎」): plain bubbles and one input (CJ, 2026-10-21: the subagent switcher is gone).
// Every step behind it is on the Agent page (#console), linked from 「看完整過程」.
import { useEffect, useState } from 'react'
import type { Event, Line } from '../contracts'
import { useWarRoom } from '../useWarRoom'
import { ApprovalList } from './ApprovalList'
import { HomeOverview } from './HomeOverview'
import { HomeStatus } from './HomeStatus'
import { useHomeSelection } from './homeSelection'
import { PlanArea } from './PlanArea'
import type { LocalItem } from './transcript'
import { setReplan, useReplans } from './replanStore'
import { ChatBar, type BarFocus, type BarMode } from './ChatBar'
import { Bubbles } from './SimpleChat'
import { useMainThread } from './mainThread'
import { useTranscript } from './useTranscript'
import { BriefingView } from '../components/BriefingView'
import { useEventBriefing } from '../useBriefing'
import './agent.css'

/** Home 主控台 (UX brief; CJ 2026-10-20: 「我的plancanvas 那樣可以嗎計劃版然後旁邊對話匡核准列表找地方放 不要懸浮了」):
 *  one status line, then three docked columns — LEFT the ranked 處理清單, CENTER the 計劃板 of the selected item
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
  const chat = (mode?: BarMode, text?: string) => { setFocus((f) => ({ mode, text, n: (f?.n ?? 0) + 1 })) }
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
    <div className={`home ${sel ? '' : 'calm'}`}>
      {!sel && <HomeOverview onFull={onFull} />}{/* nothing to decide: 今日工廠概況 in the plan column (grid-area) */}
      {/* nothing open: the list would only repeat 「全廠正常」, so the plan column takes its place (veteran 10-22 for HY);
          finished events stay in 今天處理過的事 */}
      {sel && <ApprovalList items={list} selected={sel.key} onPick={setPicked} />}
      <PlanArea sel={sel} ev={ev} items={items} opened={!!sel && picked === sel.key} onDiscuss={() => chat('discuss')} onAsk={() => chat('ask')} onCommand={(t) => chat('command', t)} />
      <Console planEv={ev} asks={asks} setAsks={setAsks} focus={focus} onFull={onFull} />
    </div>
    </>
  )
}

/** The 主控台 conversation panel: the 主 agent conversation only (CJ, 2026-10-21: 「我不要長這樣」 about the
 *  「主 agent ▸ 派出的子 agent」 switcher, so it is removed). Each subagent's progress is on the Agent page, via 看完整過程. */
function Console({ planEv, asks, setAsks, focus, onFull }: {
  planEv: Event | null
  asks: LocalItem[]; setAsks: React.Dispatch<React.SetStateAction<LocalItem[]>>; focus: BarFocus | null; onFull?: (eventId?: string) => void
}) {
  const { state } = useWarRoom()
  const thread = useMainThread(asks)
  // the input talks about the plan on screen
  const talkEv = planEv
  const waiting = !!talkEv && talkEv.state === 'await_approval' && state.pending.some((p) => p.event_id === talkEv.id)
  const full = (e: React.MouseEvent) => { if (onFull) { e.preventDefault(); onFull(talkEv?.id) } }
  // 主管摘要 (issue #93): one briefing for the plan on screen, following its state (待核准 / 執行中 / 處置完成)
  const brief = useEventBriefing(talkEv)

  return (
    <section className="console panel" aria-label="Agent Nemo 對話">
      <header className="con-h">
        <a className="con-full" href={`#console${talkEv ? `?event=${encodeURIComponent(talkEv.id)}` : ''}`} onClick={full} data-testid="see-full">看完整過程 →</a>
      </header>
      {talkEv && brief && (
        <div className="con-brief" data-testid="con-brief">
          <BriefingView b={brief} footer={brief.state !== 'pending' ? (
            <a className="br-link" href={`#console?event=${encodeURIComponent(talkEv.id)}`} onClick={full} data-testid="see-record">查看處理紀錄</a>
          ) : null} />
        </div>
      )}
      <Bubbles items={thread} empty="跟 Agent Nemo 說要做的事，或問工廠現在的狀況。" />
      <ChatBar ev={talkEv} waiting={waiting} setAsks={setAsks} focus={focus} />
    </section>
  )
}
