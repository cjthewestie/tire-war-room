// The supervisor briefing (briefing.ts) drawn once for the phone card and the desktop Agent panel.
// Section titles are <strong>; the body keeps normal weight. 「請核准」 is a reminder chip, not a button.
// Order (CJ 2026-10-21): 事件名稱與狀態 → 請核准＋期限 → 一句摘要 → sections → 收合的明細.
import type { ReactNode } from 'react'
import type { Briefing, SectionKey } from '../briefing'

/** Order ids (#A-311, SO-001) and 「28.5 h」 never break across lines; Chinese wraps naturally everywhere else. */
export function Txt({ children }: { children: string }) {
  const parts = children.split(/(#?\p{Lu}{1,3}-\d+|\d+(?:\.\d+)?\s?h\b)/gu)
  return <>{parts.map((p, i) => (i % 2 ? <span key={i} className="nb">{p.replace(/\s/g, ' ')}</span> : p))}</>
}

export function BriefTop({ b }: { b: Briefing }) {
  return (
    <>
      <p className="br-head">
        <i className="br-dot" aria-hidden="true" />
        <span className="br-name">{b.name}</span><span className="br-sep" aria-hidden="true">｜</span>
        <span className="br-st">{b.stateLabel}</span>
      </p>
      {b.deadline && (
        <p className="br-ask">
          <span className="br-tag" data-testid="br-ask">請核准</span>
          {b.deadline.time ? <span className="br-dl">請於 {b.deadline.time} 前完成核准</span> : null}
        </p>
      )}
      <p className="br-lead"><Txt>{b.lead}</Txt></p>
    </>
  )
}

export function BriefSections({ b, only, hide }: { b: Briefing; only?: SectionKey[]; hide?: RegExp }) {
  return (
    <>
      {b.sections.filter((s) => !only || only.includes(s.key)).map((s) => ({ ...s, lines: hide ? s.lines.filter((l) => !hide.test(l)) : s.lines })).filter((s) => s.lines.length).map((s) => (
        <section key={s.key} className={`br-sec br-${s.key}`}>
          <strong className="br-h">{s.title}</strong>
          {s.plain
            ? s.lines.map((l, i) => <p key={i} className="br-p"><Txt>{l}</Txt></p>)
            : <ul className="br-ul">{s.lines.map((l, i) => <li key={i}><Txt>{l}</Txt></li>)}</ul>}
        </section>
      ))}
    </>
  )
}

export function BriefMore({ b }: { b: Briefing }) {
  if (!b.details) return null
  return (
    <details className="br-more" data-testid="br-more">
      <summary>{b.details.title}</summary>
      <ul className="br-ul">{b.details.lines.map((l, i) => <li key={i}><Txt>{l}</Txt></li>)}</ul>
    </details>
  )
}

export function BriefingView({ b, footer, testId = 'briefing' }: { b: Briefing; footer?: ReactNode; testId?: string }) {
  return (
    <div className={`br br-${b.state}`} data-testid={testId} data-state={b.state}>
      <BriefTop b={b} />
      <BriefSections b={b} />
      <BriefMore b={b} />
      {footer}
    </div>
  )
}
