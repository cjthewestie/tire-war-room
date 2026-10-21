// 待核准事件 (left column of the home screen; was 核准區：等待列表). Ranked ①–⑤ (homeItems.ts); ⑤ AI 已自行處理 is collapsed.
// Selecting an item drives the 計劃 area below it and the conversation on the left.
import { ProgressRing } from '../components/ProgressRing'
import { RANK_LABEL, type HomeItem, type Rank } from './homeItems'
import { REPLAN_HINT, useReplans, type Replan } from './replanStore'

export function ApprovalList({ items, selected, onPick }: {
  items: HomeItem[]; selected: string | null; onPick: (key: string) => void
}) {
  const open = items.filter((i) => i.rank < 5)
  const fyi = items.filter((i) => i.rank === 5)
  const ranks = ([1, 2, 3, 4] as Rank[]).filter((r) => open.some((i) => i.rank === r))
  const decide = open.filter((i) => i.req).length
  const replans = useReplans()
  return (
    <section className="panel home-list" aria-label="待核准事件" data-testid="approval-list">
      <header className="hl-h"><h2>待核准事件</h2><span className={`hl-n ${decide ? 'has' : ''}`} title="待核准件數">{decide}</span></header>
      {!open.length && (
        <div className="hl-empty" data-testid="home-empty">
          <p className="hl-none">目前沒有待核准事項</p>
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
  const planning = rp?.state === 'sending' || rp?.state === 'planning'
  return (
    <button type="button" className={`hl-item tone-${i.tone} ${i.req ? `has-req r-${i.req.risk}` : ''} ${on ? 'on' : ''}`} aria-pressed={on} onClick={() => onPick(i.key)} data-testid={`home-item-${i.key}`}>
      {i.req ? (
        <>
          <span className="hl-top">
            <span className="hl-pill-row"><span className="hl-pill tone-wait">待核准</span><span className={`hl-risk r-${i.req.risk}`}>{i.req.risk}</span></span>
            <span className="hl-t">{i.machine ?? i.title}</span>
          </span>
          <span className="hl-s hl-desc">{i.desc || i.title}</span>
        </>
      ) : (
        <>
          <span className="hl-top">
            <span className="hl-pill-row">{i.progress != null && <ProgressRing value={i.progress} tone={i.progress >= 1 ? 'done' : 'live'} size={20} title={`完成 ${Math.round(i.progress * 100)}%`} />}<span className={`hl-pill tone-${planning ? 'run' : i.tone}`}>{planning ? 'AI 規劃中…' : rp?.state === 'ready' ? '已提出方案' : i.pill}</span></span>
            <span className="hl-t">{i.title}</span>
          </span>
          <span className="hl-s">{i.sub}</span>
        </>
      )}
      {rp === null && <span className="hl-hint" data-testid="item-replan-hint">{REPLAN_HINT}</span>}
      {planning && <span className="hl-hint live" data-testid="item-planning"><i className="spin" />AI 規劃中…完成後會自動跳到核准卡</span>}
      {rp?.state === 'ready' && <span className="hl-hint">方案已送出，見 ③ 的待核准卡</span>}
      {rp?.state === 'failed' && <span className="hl-hint err">AI 沒有開始規劃：{rp.error}</span>}
      {rp?.state === 'closed' && <span className="hl-hint" data-testid="item-replan-closed">AI 已處理，沒有需要核准的方案：{rp.outcome ?? '已結案'}</span>}
    </button>
  )
}
