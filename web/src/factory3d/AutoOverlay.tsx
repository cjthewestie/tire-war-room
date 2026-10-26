// Auto-play demo overlay (/factory?demo=cure07, or ?mock=1&demo=cure07): one caption line on top (the beat's, demo/beat.ts)
// and one small 「下一階段 ▶」 button (CJ 10-22; also N / Space / →) that skips to the next beat now — the demo's own
// pending card is approved on the way. No pause / replay (「不需要下面暫停跟重新播放」); Esc still leaves.
import { AUTO_TEXT } from '../demo/auto'
import { stageOf, useDemoBeat } from '../demo/beat'
import type { GuidedDemo } from './useGuidedDemo'

export function AutoOverlay({ demo }: { demo: GuidedDemo }) {
  const beat = useDemoBeat()
  const title = beat ? AUTO_TEXT[beat] : demo.cue.title
  const stage = stageOf(beat)
  return (
    <>
      <div className="f3-auto-cap" data-testid="auto-cap" data-beat={beat ?? undefined}>{title}</div>
      {stage && (
        <div className="f3-auto-stage" data-testid="auto-stage">
          <span>{stage.n}/{stage.total} {stage.label}</span>
          {beat !== 'back' && (
            <button type="button" className="f3-auto-next" data-testid="auto-next" onClick={() => void demo.cont()} title="跳到下一階段（N / →）">
              下一階段 ▶
            </button>
          )}
        </div>
      )}
    </>
  )
}
