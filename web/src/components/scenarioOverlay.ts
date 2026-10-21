// 情境疊加 (scenario overlay): what a demo scenario button does while something is already happening.
// While the running scenario still has an open event or a pending approval, a click on a DIFFERENT overlayable scenario
// adds its faults on top from now (POST /api/sim/control {action:'overlay'}; the mock engine does the same) instead of
// rebuilding the plant. The same scenario again (also once it has been overlaid), ↺ 重來, a story scenario, or a click
// with nothing open loads as before: docs/DEMO-SCRIPT.md's rescue 「click 7 號硫化機溫降 again」 still starts it fresh.
// Used by the demo strip, the 3D presenter keys and the 3D 觸發異常 menu.
import type { OverlayResult, ScenarioName } from '../contracts'
import type { WarRoomState } from '../store'

/** Pure-fault / plant-input scenarios, mirrors warroom/sim/scenarios.py OVERLAY_SCENARIOS. The story scenarios
 *  (press7_temp_drop p.11, day_loop 一日循環, shift_start 早班開工, normal 正常運轉) always reload. */
export const OVERLAYABLE: ReadonlySet<ScenarioName> = new Set<ScenarioName>(['concurrent_3', 'open_mill_overheat', 'bad_data', 'rush_order', 'staffing'])

/** An event not yet closed / safe-stopped, or an approval still waiting. */
export function hasOpenWork(state: WarRoomState): boolean {
  return (state.snapshot?.events ?? []).some((e) => e.state !== 'closed' && e.state !== 'safe_stop') || state.pending.length > 0
}

/** The loaded scenario and the ones overlaid on it, from the clock message (live and mock both send them). */
export function runningScenarios(state: WarRoomState, fallback: ScenarioName | null = null): { loaded: ScenarioName | null; overlays: ScenarioName[] } {
  const c = state.clock as (WarRoomState['clock'] & { overlays?: ScenarioName[] }) | null
  return { loaded: (c?.scenario as ScenarioName | undefined) ?? fallback, overlays: c?.overlays ?? [] }
}

/** 'overlay' or 'load' for a click on scenario `name`. */
export function scenarioClick(name: ScenarioName, state: WarRoomState, fallback: ScenarioName | null = null): 'overlay' | 'load' {
  const { loaded, overlays } = runningScenarios(state, fallback)
  if (!OVERLAYABLE.has(name) || !loaded || name === loaded || overlays.includes(name)) return 'load'
  return hasOpenWork(state) ? 'overlay' : 'load'
}

/** How long the 3D page's demo note shows an overlay line (it has substitution notes to read). */
export const OVERLAY_NOTE_MS = 6000

/** 「已疊加：3 件同時異常（新增 3 件）」, then one note per substituted or skipped step. */
export function overlayNote(label: string, r: OverlayResult): string {
  const notes = r.steps.map((s) => s.detail).filter((d): d is string => !!d)
  const quiet = r.steps.filter((s) => !s.wakes && !s.skipped).length
  const head = `已疊加：${label.replace(/（.*?）/, '')}（新增 ${r.new_events} 件${quiet ? `；${quiet} 筆壞資料不喚醒 AI` : ''}）`
  return [head, ...notes].join('・')
}
