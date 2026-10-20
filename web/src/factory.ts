// Static factory data the UI needs for layout, mirrored from warroom/factory.py.
// tests/test_web.py checks these against Python, so keep the two in sync.
import type { Line, MachineId, TeamId, TireSize } from './contracts'

export interface MachineMeta { id: MachineId; line: Line; name: string; short: string }

export const MACHINES: MachineMeta[] = [
  { id: 'A-MIX', line: 'A', name: '密煉機', short: '密煉' },
  { id: 'A-OPEN', line: 'A', name: '開煉機', short: '開煉' },
  { id: 'A-BIGOPEN', line: 'A', name: '大開煉機', short: '大開煉' },
  { id: 'A-SMALLOPEN', line: 'A', name: '小開煉機', short: '小開煉' },
  { id: 'A-CAL', line: 'A', name: '壓延機', short: '壓延' },
  { id: 'A-COOL', line: 'A', name: '冷卻機', short: '冷卻' },
  { id: 'A-WIND', line: 'A', name: '捲取設備', short: '捲取' },
  { id: 'B-MIX', line: 'B', name: '密煉機', short: '密煉' },
  { id: 'B-OPEN', line: 'B', name: '開煉機', short: '開煉' },
  { id: 'B-FORM', line: 'B', name: '油壓機', short: '成形' },
  { id: 'B-ASSY', line: 'B', name: '油壓機', short: '組裝' },
  { id: 'C-PAYOFF', line: 'C', name: '放線架', short: '放線' },
  { id: 'C-EXTR', line: 'C', name: '押出機', short: '押出' },
  { id: 'C-WIND', line: 'C', name: '纏繞機', short: '纏繞' },
  { id: 'C-WRAP', line: 'C', name: '氣囊', short: '包布' },
  { id: 'D-CUT', line: 'D', name: '裁切機', short: '裁切' },
  { id: 'BOND-01', line: 'BOND', name: '油壓機', short: '油壓' },
  ...(['01', '02', '03', '04', '05', '06', '07', '08', '09', '10', '11'] as const).map((n) => ({
    id: `CURE-${n}` as MachineId, line: 'CURE' as Line, name: '硫化機', short: String(Number(n)),
  })),
]
export const MACHINE_META: Record<string, MachineMeta> = Object.fromEntries(MACHINES.map((m) => [m.id, m]))
export const LINES: Line[] = ['A', 'B', 'C', 'D', 'BOND', 'CURE']
export const LINE_SHORT: Record<Line, string> = { A: 'A', B: 'B', C: 'C', D: 'D', BOND: '貼合', CURE: '硫化' }

export const CURE_PRESS_IDS = MACHINES.filter((m) => m.line === 'CURE').map((m) => m.id)
/** Architect decision: CURE-07..10 are the 4 shared medium/large presses (page 11 wins over page 3). */
export const ML_PRESSES = ['CURE-08', 'CURE-09', 'CURE-10', 'CURE-11']
export const STEAM_HEADERS: Record<string, string[]> = {
  H1: ['CURE-01', 'CURE-02', 'CURE-03', 'CURE-04', 'CURE-05'],
  H2: ['CURE-06', 'CURE-07', 'CURE-08'],
  H3: ['CURE-09', 'CURE-10', 'CURE-11'],
}
export function machineLabel(id: string): string {
  const m = MACHINE_META[id]
  if (!m) return id === 'PLANT' ? '全廠' : id   // issue #17 plant events / commands about the whole plant
  if (m.line === 'CURE') return `${m.short} 號硫化機`
  if (m.line === 'BOND') return '貼合油壓機'
  return `${m.line} 線${m.name}`
}

export const CURE_HOURS: Record<TireSize, number> = { S: 24, M: 28, L: 34 }
export const SIZE_LABEL: Record<TireSize, string> = { S: '小胎', M: '中胎', L: '大胎' }

export const TEAM_LABELS: Record<TeamId, string> = {
  curing: '硫化機組', body: '胎體／胎面膠組', side: '胎側膠組', bead_nylon: '鋼絲／尼龍組',
  bigopen: '大開煉機組', bond: '貼合組', maintenance: '維修組', qc: '品管', supervisor: '主管',
}
/** Page 4 row order. */
export const LABOR_TEAMS: TeamId[] = ['curing', 'body', 'side', 'bead_nylon', 'bigopen', 'bond']

export interface ShiftDef { team: TeamId; name: string; start: number; end: number; headcount: number }
// Page 4: most teams day shift only; 胎體/胎面膠組 also has a night shift.
export const CURRENT_SHIFTS: ShiftDef[] = [
  { team: 'curing', name: '早班', start: 8, end: 17, headcount: 2 },
  { team: 'body', name: '早班', start: 8, end: 17, headcount: 2 },
  { team: 'body', name: '晚班', start: 20, end: 5, headcount: 2 },
  { team: 'side', name: '早班', start: 8, end: 17, headcount: 2 },
  { team: 'bead_nylon', name: '早班', start: 8, end: 17, headcount: 2 },
  { team: 'bigopen', name: '早班', start: 8, end: 17, headcount: 2 },
  { team: 'bond', name: '早班', start: 8, end: 17, headcount: 2 },
]
// Page 6 premise: curing staggered 6–15 and 14–23; body night 20–05 also de-molds / molds in.
export const OPTIMISED_SHIFTS: ShiftDef[] = [
  { team: 'curing', name: '早班', start: 6, end: 15, headcount: 2 },
  { team: 'curing', name: '中班', start: 14, end: 23, headcount: 2 },
  { team: 'body', name: '早班', start: 8, end: 17, headcount: 2 },
  { team: 'body', name: '夜班', start: 20, end: 5, headcount: 2 },
  { team: 'side', name: '早班', start: 8, end: 17, headcount: 2 },
  { team: 'bead_nylon', name: '早班', start: 8, end: 17, headcount: 2 },
  { team: 'bigopen', name: '早班', start: 8, end: 17, headcount: 2 },
  { team: 'bond', name: '早班', start: 8, end: 17, headcount: 2 },
]

/** Page 6 weekly output per press. */
export const CAPACITY_TARGETS = [
  { label: '現況', small: 6, medium: 5, large: 3 },
  { label: '只延長班表', small: 6, medium: 5.9, large: 4.5 },
  { label: 'AI 排出模時刻', small: 7, medium: 6, large: 4.9 },
]
export const LEAD_TIME_WEEKS = { ml: [13.3, 9.3] as [number, number], small: [2.4, 2.0] as [number, number] }
export const CAPACITY_CONDITIONS = '週一至週六；週六夜班做到週日 05:00；中、大胎各 2 台；不計換模；出模／入模 30 分'

/** Page 2 demo numbers: done / total / hours left. */
export const DEMO_ORDER_PROGRESS: Record<TireSize, [number, number, number]> = {
  S: [38, 100, 208], M: [12, 100, 1241], L: [8, 100, 2162],
}

export const COMPOUND_STOP_C = 120
export const CURATIVE_STOP_C = 110
export const CURE_LOW_STOP_C = 120
