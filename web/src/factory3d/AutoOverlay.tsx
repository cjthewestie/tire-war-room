// Auto-play demo overlay (/factory?demo=cure07, ?demo=simple, or ?mock=1&demo=cure07): one caption line on top (the
// beat's, demo/beat.ts) and one small 「下一階段 ▶」 button (CJ 10-22; also N / Space / →) that skips to the next beat now
// — the demo's own pending card is approved on the way. No pause / replay (「不需要下面暫停跟重新播放」); Esc still leaves.
// The simple version (?demo=simple, CJ 10-27) shows its own 7-stage captions and a 「n/7」 chip; no dock, no prompter.
import { AUTO_TEXT, SIMPLE_TEXT, simpleRequested, simpleStageOf } from '../demo/auto'
import { useDemoBeat } from '../demo/beat'
import type { GuidedDemo } from './useGuidedDemo'

export function AutoOverlay({ demo }: { demo: GuidedDemo }) {
  const beat = useDemoBeat()
  const simple = simpleRequested()
  const title = beat ? (simple ? SIMPLE_TEXT : AUTO_TEXT)[beat] : demo.cue.title
  const stage = simple ? simpleStageOf(beat) : null
  return (
    <>
      <div className="f3-auto-cap" data-testid="auto-cap" data-beat={beat ?? undefined} data-stage={stage?.n}>
        {stage && <span className="f3-simple-stage" data-testid="simple-stage">{stage.n}/{stage.total}</span>}
        {title}
      </div>
    </>
  )
}
