// 核准區：等待列表 (right-top of the home screen). Ranked ①–⑤ (homeItems.ts); ⑤ AI 已自行處理 is collapsed.
// Selecting an item drives the 計劃 area below it and the conversation on the left.
import { ProgressRing } from '../components/ProgressRing'
import { RANK_LABEL, type HomeItem, type Rank } from './homeItems'
import { REPLAN_HINT, useReplans, type Replan } from './replanStore'
import { useWarRoom } from '../useWarRoom'
import { SUB_STATUS } from './subagents'

export function ApprovalList({ items, selected, onPick, next }: {
  items: HomeItem[]; selected: string | null; onPick: (key: string) => void; next: string[]
}) {
  const open = items.filter((i) => i.rank < 5)
  const fyi = items.filter((i) => i.rank === 5)
  const ranks = ([1, 2, 3, 4] as Rank[]).filter((r) => open.some((i) => i.rank === r))
  const decide = open.filter((i) => i.req).length
  const replans = useReplans()
  return (
    <section className="panel home-list" aria-label="核准區：等待列表" data-testid="approval-list">
      <header className="hl-h"><h2>核准區：等待列表</h2><span className={`hl-n ${decide ? 'has' : ''}`} title="待核准件數">{decide}</span></header>
      {!open.length && (
        <div className="hl-empty" data-testid="home-empty">
          <p className="calm-title">全廠正常，沒有要你處理的事</p>
          {next.length > 0 && <p className="muted">接下來：{next.slice(0, 3).join('・')}</p>}
        </div>
      )}
      <div className="hl-body">
        {ranks.map((r) => (
          <div key={r} className="hl-sec">
            <h3 className="hl-k">{RANK_LABEL[r]}</h3>
            {open.filter((i) => i.rank === r).map((i) => <Row key={i.key} i={i} on={i.key === selected} onPick={onPick} rp={i.orderId ? replans[i.orderId] ?? null : undefined} />)}
          </div>
        ))}
        {fyi.length > 0 && (
          <details className="hl-fyi">
            <summary>{RANK_LABEL[5]}（{fyi.length}）</summary>
            {fyi.slice(0, 12).map((i) => <Row key={i.key} i={i} on={i.key === selected} onPick={onPick} />)}
          </details>
        )}
      </div>
    </section>
  )
}

/** `rp`: a ② item's replan (null = not asked yet → the hint line; undefined = not an order item). */
function Row({ i, on, onPick, rp }: { i: HomeItem; on: boolean; onPick: (key: string) => void; rp?: Replan | null }) {
  const sub = useWarRoom().state.subagents?.items.find((s) => s.id === i.eventId)
  const planning = rp?.state === 'sending' || rp?.state === 'planning'
  return (
    <button type="button" className={`hl-item tone-${i.tone} ${on ? 'on' : ''}`} aria-pressed={on} onClick={() => onPick(i.key)} data-testid={`home-item-${i.key}`}>
      <span className="hl-top">
        <span className="hl-pill-row">{i.progress != null && <ProgressRing value={i.progress} tone={i.progress >= 1 ? 'done' : 'live'} size={20} title={`完成 ${Math.round(i.progress * 100)}%`} />}<span className={`hl-pill tone-${planning ? 'run' : i.tone}${i.back ? ' is-back' : ''}`}>{planning ? 'Agent Nemo 規劃中…' : rp?.state === 'ready' ? '計劃待核准' : i.pill}</span></span>
        <span className="hl-t">{i.title}</span>
      </span>
      <span className="hl-s">{i.sub}</span>
      {sub && !i.back && <span className={`hl-agent s-${sub.status}`} data-testid="item-subagent">{['running', 'waiting_tasks'].includes(sub.status) && i.rank === 4 ? '執行中' : SUB_STATUS[sub.status]}</span>}
      {rp === null && <span className="hl-hint" data-testid="item-replan-hint">{REPLAN_HINT}</span>}
      {planning && <span className="hl-hint live" data-testid="item-planning"><i className="spin" />Agent Nemo 規劃中…好了會自動切到核准卡</span>}
      {rp?.state === 'ready' && <span className="hl-hint">Agent Nemo 的計劃已送出，見 ③ 的待核准卡</span>}
      {rp?.state === 'failed' && <span className="hl-hint err">Agent Nemo 沒有開始規劃：{rp.error}</span>}
      {rp?.state === 'closed' && <span className="hl-hint" data-testid="item-replan-closed">Agent Nemo 規劃完，沒有需要核准的變更：{rp.outcome ?? '已結案'}</span>}
    </button>
  )
}
