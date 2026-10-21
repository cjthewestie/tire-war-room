// Home 主控台: the 核准區 list, the plan of the selected item (核准並派發 / 討論), and the conversation with the 主 agent
// (issue #54; CJ, 2026-10-20: 「單純一點好嗎」): plain bubbles and one input (CJ, 2026-10-21: the subagent switcher is gone).
// Every step behind it is on the Agent page (#console), linked from 「看完整過程」.
import { useEffect, useMemo, useState } from 'react'
import type { Event, Line } from '../contracts'
import { useWarRoom } from '../useWarRoom'
import { ApprovalList } from './ApprovalList'
import { HomeStatus } from './HomeStatus'
import { nextUp } from './homeItems'
import { useHomeSelection } from './homeSelection'
import { PlanArea } from './PlanArea'
import type { LocalItem } from './transcript'
import { setReplan, useReplans } from './replanStore'
import { ChatBar, type BarFocus, type BarMode } from './ChatBar'
import { Bubbles } from './SimpleChat'
import { useMainThread } from './mainThread'
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
  const [focus, setFocus] = useState<BarFocus | null>(null)
  const chat = (mode?: BarMode, text?: string) => { setFocus((f) => ({ mode, text, n: (f?.n ?? 0) + 1 })) }
  const next = useMemo(() => nextUp(state.schedule, state.world, now), [state.schedule, state.world, now])
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
      <ApprovalList items={list} selected={sel?.key ?? null} onPick={setPicked} next={next} />
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

  return (
    <section className="console panel" aria-label="NemoClaw 對話">
      <header className="con-h">
        <a className="con-full" href={`#console${talkEv ? `?event=${encodeURIComponent(talkEv.id)}` : ''}`} onClick={full} data-testid="see-full">看完整過程 →</a>
      </header>
      <Bubbles items={thread} empty="跟NemoClaw 說要做的事，或問工廠現在的狀況。" />
      <ChatBar ev={talkEv} waiting={waiting} setAsks={setAsks} focus={focus} />
    </section>
  )
}
