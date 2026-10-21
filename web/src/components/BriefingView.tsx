// The supervisor briefing (briefing.ts) drawn once for the phone card and the desktop Agent panel.
// Section titles are <strong>; the body keeps normal weight. 「請核准」 is a reminder chip, not a button.
import type { ReactNode } from 'react'
import type { Briefing } from '../briefing'

export function BriefingView({ b, footer, testId = 'briefing' }: { b: Briefing; footer?: ReactNode; testId?: string }) {
  return (
    <div className={`br br-${b.state}`} data-testid={testId} data-state={b.state}>
      <p className="br-head">
        <i className="br-dot" aria-hidden="true" />
        <span className="br-name">{b.name}</span><span className="br-sep" aria-hidden="true">｜</span>
        <span className="br-st">{b.stateLabel}</span>
      </p>
      <p className="br-lead">{b.lead}</p>
      {b.sections.map((s) => (
        <section key={s.key} className={`br-sec br-${s.key}`}>
          <strong className="br-h">{s.title}</strong>
          {s.plain
            ? s.lines.map((l, i) => <p key={i} className="br-p">{l}</p>)
            : <ul className="br-ul">{s.lines.map((l, i) => <li key={i}>{l}</li>)}</ul>}
        </section>
      ))}
      {b.deadline && (
        <section className="br-sec br-deadline">
          <strong className="br-h">核准期限</strong>
          <p className="br-p"><span className="br-tag" data-testid="br-ask">請核准</span>{b.deadline.time ? <span className="br-dl"> 請於 {b.deadline.time} 前完成核准。</span> : null}</p>
          <p className="br-p br-hint">如需調整，可直接告訴我你的限制。</p>
        </section>
      )}
      {footer}
    </div>
  )
}
