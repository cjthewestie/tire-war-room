// How loud each machine's alarm is in the 3D factory (CJ, 2026-10-22: 「3d戰情室的那個異常警示燈太不明顯了」), pure so
// e2e/alarm-unit.mjs can check it. Severity colours match the war room: amber = anomaly / AI handling, blue-amber blink =
// waiting for the supervisor's approval, red = Safety Core stop or SAFE_STOP, green = just resolved (a short flash).
import type { ApprovalRequest, Event, MachineTile } from '../contracts'
import { machineLabel } from '../factory'

export type AlarmLevel = 'anomaly' | 'approval' | 'stop'
export interface Alarm { level: AlarmLevel; text: string; eventId: string | null }

export const ALARM_HEX: Record<AlarmLevel | 'resolved' | 'blue', string> = {
  anomaly: '#f5a623', approval: '#f5a623', stop: '#ff3b30', resolved: '#3fcf5a', blue: '#4f9be0',
}
const ICON: Record<AlarmLevel, string> = { anomaly: '⚠', approval: '⏳', stop: '⛔' }
const KIND: Record<string, string> = {
  overheat: '過熱', temp_drop: '溫降', pressure_low: '壓力低', water_flow_low: '冷卻水低', steam_low: '蒸汽壓低',
  sensor_fault: '感測器異常', multi_machine: '多台異常', schedule_slip: '進度落後',
}
const open = (e: Event) => e.state !== 'closed' && e.state !== 'safe_stop'

/** Every abnormal machine and its alarm; machines missing from the result are normal. */
export function alarms(tiles: MachineTile[] | undefined, events: Event[] | undefined, pending: ApprovalRequest[]): Record<string, Alarm> {
  const out: Record<string, Alarm> = {}
  const evs = (events ?? []).filter((e) => e.machine_id && e.machine_id !== 'PLANT')
  const waiting = new Set(pending.map((p) => p.event_id))
  const tileOf = Object.fromEntries((tiles ?? []).map((t) => [t.machine_id, t]))
  const temp = (id: string) => (tileOf[id]?.temp_c != null ? ` ${tileOf[id].temp_c!.toFixed(0)}°C` : '')
  // newest first, so a machine shows its latest event
  for (const e of [...evs].sort((a, b) => (a.opened_at < b.opened_at ? 1 : -1))) {
    const id = e.machine_id
    if (out[id]) continue
    const stopped = e.state === 'safe_stop' || (open(e) && e.safety_actions.some((s) => s.action === 'stop_machine'))
    if (stopped) { out[id] = { level: 'stop', text: `${ICON.stop} ${machineLabel(id)} 安全核心停機`, eventId: e.id }; continue }
    if (!open(e)) continue
    if (e.state === 'await_approval' || waiting.has(e.id)) {
      out[id] = { level: 'approval', text: `${ICON.approval} ${machineLabel(id)} 等主管核准`, eventId: e.id }
      continue
    }
    out[id] = { level: 'anomaly', text: `${ICON.anomaly} ${machineLabel(id)} ${KIND[e.anomaly?.kind] ?? '異常'}${temp(id)}`, eventId: e.id }
  }
  // a machine stopped or alarming with no event (Safety Core acts before the AI): still loud
  for (const t of tiles ?? []) {
    if (out[t.machine_id]) continue
    if (t.status === 'stopped') out[t.machine_id] = { level: 'stop', text: `${ICON.stop} ${machineLabel(t.machine_id)} 停機`, eventId: null }
    else if (t.status === 'alarm') out[t.machine_id] = { level: 'anomaly', text: `${ICON.anomaly} ${machineLabel(t.machine_id)} 異常${temp(t.machine_id)}`, eventId: null }
  }
  return out
}

/** Machines that just stopped being abnormal (for the green flash): in `before`, not in `now`. */
export function resolvedNow(before: Record<string, Alarm>, now: Record<string, Alarm>): string[] {
  return Object.keys(before).filter((id) => !now[id])
}

/** Pulse speed: slower with reduced motion (the colours stay). */
export const pulseRate = (reduced: boolean) => (reduced ? 0.35 : 1)

/** The DOM arrows FactoryApp draws for off-screen alarms, by machine id; Alarms.tsx moves them every frame. */
export const arrowEls = new Map<string, HTMLElement>()
