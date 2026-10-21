// 核准卡「怎麼選」 note (logic in optionGuide.ts), shared by the phone card and the dashboard card.
import type { ApprovalRequest, Event } from './contracts'
import { useHealth } from './health'
import { optionGuide } from './optionGuide'
import { fastApplies } from './phone/fastEta'
import { useWarRoom } from './useWarRoom'

export function OptionGuideNote({ a, ev, className }: { a: ApprovalRequest; ev: Event | null; className?: string }) {
  const fastS = useHealth()?.repair_fast_s ?? null
  const tasks = useWarRoom().state.world?.tasks.items ?? []
  // fast repair, for a machine the crew fixes: the repair seconds, not the verification window
  const g = optionGuide(a, ev, fastS && ev && fastApplies(ev, tasks.filter((t) => t.event_id === ev.id)) ? fastS : null)
  if (!g) return null
  return (
    <div className={`opt-guide ${className ?? ''}`} data-testid="option-guide">
      <p><b>怎麼選：</b>{g.head}</p>
      {g.fix && <p><b>解除異常靠：</b>{g.fix}</p>}
      <ul>{g.lines.map((l) => <li key={l.id}><b>{l.id}</b> {l.text}</li>)}</ul>
      {g.rec && <p className="opt-guide-rec">{g.rec}</p>}
    </div>
  )
}
