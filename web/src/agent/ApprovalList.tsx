// 核准區：等待列表 (right-top of the home screen). Ranked ①–⑤ (homeItems.ts); ⑤ AI 已自行處理 is collapsed.
// Selecting an item drives the 計劃 area below it and the conversation on the left.
import { RANK_LABEL, type HomeItem, type Rank } from './homeItems'

export function ApprovalList({ items, selected, onPick, next }: {
  items: HomeItem[]; selected: string | null; onPick: (key: string) => void; next: string[]
}) {
  const open = items.filter((i) => i.rank < 5)
  const fyi = items.filter((i) => i.rank === 5)
  const ranks = ([1, 2, 3, 4] as Rank[]).filter((r) => open.some((i) => i.rank === r))
  const decide = open.filter((i) => i.req).length
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
            {open.filter((i) => i.rank === r).map((i) => <Row key={i.key} i={i} on={i.key === selected} onPick={onPick} />)}
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

function Row({ i, on, onPick }: { i: HomeItem; on: boolean; onPick: (key: string) => void }) {
  return (
    <button type="button" className={`hl-item tone-${i.tone} ${on ? 'on' : ''}`} aria-pressed={on} onClick={() => onPick(i.key)} data-testid={`home-item-${i.key}`}>
      <span className="hl-top"><span className={`hl-pill tone-${i.tone}`}>{i.pill}</span><span className="hl-t">{i.title}</span></span>
      <span className="hl-s">{i.sub}</span>
    </button>
  )
}
