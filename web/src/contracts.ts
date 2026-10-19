// Hand-written mirror of warroom/contracts.py (API-facing models only).
// tests/test_contracts.py checks that every enum value, REST path and machine id below
// matches Python. Change both files together.
// Datetimes are ISO 8601 strings with +08:00 offset (factory time, Asia/Taipei).

export type ISODate = string

// ---------------- enums ----------------
export type Line = "A" | "B" | "C" | "D" | "BOND" | "CURE"
export type TireSize = "S" | "M" | "L"
export type TeamId = "curing" | "body" | "side" | "bead_nylon" | "bigopen" | "bond" | "maintenance" | "qc" | "supervisor"
export type MachineStatus = "running" | "idle" | "warning" | "alarm" | "stopped" | "out_of_service" | "offline"
export type CompoundStage = "none" | "unvulcanized" | "curative_added"
export type Channel = "temp_c" | "pressure_kgf_cm2" | "water_flow_lpm" | "steam_kgf_cm2"
export type DataQuality = "ok" | "out_of_range" | "jump" | "stale" | "neighbor_mismatch" | "missing"
export type AnomalyKind = "overheat" | "temp_drop" | "pressure_low" | "water_flow_low" | "steam_low" | "sensor_fault" | "multi_machine" | "schedule_slip"
export type Severity = "info" | "warn" | "critical"
export type TileStatus = "normal" | "warn" | "alarm"
export type EventState =
  | "detected" | "classify" | "plan" | "simulate" | "gate" | "await_approval"
  | "execute" | "verify" | "replan" | "closed" | "safe_stop"
export type ToolName =
  | "read_sensors" | "predict_anomaly" | "cure_formula" | "solve_schedule" | "sop_search"
  | "query_orders_shifts" | "confirm_schedule" | "set_cure_time" | "notify_teams"
/** Tools that change the plant (mirrors contracts.py WRITE_TOOLS). */
export const WRITE_TOOLS: readonly ToolName[] = ["confirm_schedule", "set_cure_time", "notify_teams"]
export type RiskLevel = "R0" | "R1" | "R2" | "R3"
export type GateVerdict = "allow" | "needs_approval" | "reject"
export type ApprovalStatus = "pending" | "approved" | "modified" | "rejected" | "expired"
export type DecisionKind = "approve" | "modify" | "reject"
export type HypothesisStatus = "proposed" | "active" | "rejected" | "confirmed"
export type SafetyActionKind = "stop_machine" | "hold_batch" | "cure_hold" | "alarm_only"
export type ScheduleStatus = "draft" | "proposed" | "active" | "superseded" | "rolled_back"
export type SolverName = "cp-sat" | "heuristic"
export type LLMMode = "llm" | "mock"
export type ScenarioName = "normal" | "press7_temp_drop" | "open_mill_overheat" | "bad_data" | "concurrent_3"
export type AuditKind =
  | "state_transition" | "llm_call" | "tool_call" | "tool_result" | "gate" | "intercepted"
  | "approval_request" | "approval_decision" | "safety_action" | "anomaly" | "schedule_version"
  | "rollback" | "notify" | "system"
export type WsType =
  | "snapshot" | "machines" | "anomaly" | "safety_action" | "event" | "alert_card"
  | "approval_request" | "approval_resolved" | "schedule" | "audit" | "clock" | "world"

export const MACHINE_IDS = [
  "A-MIX", "A-OPEN", "A-BIGOPEN", "A-SMALLOPEN", "A-CAL", "A-COOL", "A-WIND",
  "B-MIX", "B-OPEN", "B-FORM", "B-ASSY",
  "C-PAYOFF", "C-EXTR", "C-WIND", "C-WRAP",
  "D-CUT",
  "BOND-01",
  "CURE-01", "CURE-02", "CURE-03", "CURE-04", "CURE-05", "CURE-06",
  "CURE-07", "CURE-08", "CURE-09", "CURE-10", "CURE-11",
] as const
export type MachineId = (typeof MACHINE_IDS)[number]

export const LINE_LABELS: Record<Line, string> = {
  A: "A 胎體／胎面膠", B: "B 胎側膠", C: "C 鋼絲圈", D: "D 尼龍布", BOND: "貼合", CURE: "硫化",
}

// ---------------- models ----------------
export interface SensorReading {
  ts: ISODate
  machine_id: string
  temp_c: number | null
  pressure_kgf_cm2: number | null
  water_flow_lpm: number | null
  steam_kgf_cm2: number | null
  seq: number
  quality: DataQuality
}

export interface CureJobState {
  job_id: string
  order_id: string
  size: TireSize
  mold_id: string
  mold_in_at: ISODate
  planned_demold_at: ISODate
  setpoint_c: number
  cure_required_h: number
  cure_elapsed_h: number
}

export interface MachineState {
  machine_id: string
  line: Line
  name: string
  short_name: string
  status: MachineStatus
  last_reading: SensorReading | null
  step_elapsed_min: number
  compound_stage: CompoundStage
  batch_id: string | null
  cure_job: CureJobState | null
  active_event_id: string | null
  note: string | null
}

export interface SemiFinishedPart {
  part: "A" | "B" | "C" | "D"
  label: string
  status_text: string
  state: "waiting" | "in_progress" | "done" | "alert"
  machine_id: string | null
}

export interface SemiFinishedProgress {
  tire_label: string
  order_id: string
  size: TireSize
  parts: SemiFinishedPart[]
  planned_mold_in_at: ISODate | null
  note?: string | null
  note_state?: "waiting" | "in_progress" | "done" | "alert" | null
}

export interface Order {
  id: string
  size: TireSize
  qty: number
  done: number
  due: string // YYYY-MM-DD
  customer: string | null
  mold_id: string | null
  priority: number
}

export interface Shift {
  id: string
  team: TeamId
  name: string
  start: ISODate
  end: ISODate
  headcount: number
  required: number | null
}

export interface Prediction {
  machine_id: string
  channel: Channel
  now_value: number
  predicted_value: number
  band_lo: number
  band_hi: number
  horizon_s: number
  trend_per_min: number
  model: string
}

export interface Anomaly {
  id: string
  ts: ISODate
  machine_id: string
  kind: AnomalyKind
  channel: Channel
  value: number
  baseline: number
  prediction: Prediction | null
  severity: Severity
  message: string
  related_machines: string[]
  detection_latency_s: number | null
}

export interface SafetyAction {
  id: string
  ts: ISODate
  machine_id: string
  rule_id: string
  action: SafetyActionKind
  value: number
  threshold: number
  message: string
  latency_ms: number
}

// 問戰情室: the supervisor's read-only question (POST /api/ask)
export interface AskStepResult {
  tool: string
  args: Record<string, unknown>
  ok: boolean
  error: string | null
  summary: string
}

export interface AskAnswer {
  id: string // "ASK-0001": the audit entries carry it as event_id
  question: string
  answer: string
  steps: AskStepResult[]
  intercepted: { tool: string; args: Record<string, unknown>; state: string; reason: string }[]
  mode: LLMMode
  fell_back_to_mock: boolean
  factory_time: string
  total_ms: number
}

export interface ToolResult {
  call_id: string
  tool: ToolName
  ok: boolean
  data: Record<string, unknown>
  error: string | null
  duration_ms: number
  attempt: number
}

export interface Expectation {
  machine_id: string
  channel: Channel
  op: ">=" | "<=" | "between"
  value: number
  value_hi: number | null
  within_min: number
}

export interface Hypothesis {
  id: string
  statement: string
  cause_code: string
  expectation: Expectation | null
  status: HypothesisStatus
  evidence: string[]
}

export interface PlanStep {
  id: string
  tool: ToolName
  args: Record<string, unknown>
  depends_on: string[]
  purpose: string
}

export interface AlertCard {
  title: string
  summary: string
}

export interface CommanderPlan {
  goal: string
  hypothesis: Hypothesis
  rejected_hypothesis_ids: string[]
  steps: PlanStep[]
  alert_card: AlertCard
  safe_stop: boolean
  rationale: string
}

export interface GateDecision {
  step_id: string
  tool: ToolName
  verdict: GateVerdict
  risk: RiskLevel | null
  checks: Record<string, boolean>
  reasons: string[]
}

export interface ApprovalOption {
  id: string
  label: string
  tool: ToolName
  args: Record<string, unknown>
  recommended: boolean
}

export interface ApprovalDecision {
  request_id: string
  decision: DecisionKind
  option_id?: string | null
  modified_args?: Record<string, unknown> | null
  reason?: string
  decided_by?: string
  via?: "phone" | "touchscreen" | "dashboard" | "auto"
  decided_at?: ISODate | null
}

export interface ApprovalRequest {
  id: string
  event_id: string
  step_id: string | null
  round: number
  created_at: ISODate
  risk: RiskLevel
  title: string
  reason: string
  done: string[]
  gaps: string[]
  delivery_impact: string
  actions?: string[]
  options: ApprovalOption[]
  status: ApprovalStatus
  decision: ApprovalDecision | null
  expires_at: ISODate | null
}

export interface AuditEntry {
  seq: number
  ts: ISODate
  kind: AuditKind
  actor: string
  event_id: string | null
  payload: Record<string, unknown>
  prev_hash: string
  hash: string
}

export type TimelineActor = "事件偵測" | "安全核心" | "指揮官" | "工具" | "求解器" | "主管" | "驗證（程式）" | "系統"
export interface TimelineItem {
  ts: ISODate
  actor: TimelineActor
  text: string
  tag: "tool" | "human" | "safety" | "decision" | "dynamic" | "info"
}

export interface StepRun {
  step: PlanStep
  state: "pending" | "running" | "ok" | "failed" | "skipped" | "awaiting_approval" | "rejected"
  result: ToolResult | null
  gate: GateDecision | null
}

export interface StepRound {
  round: number
  hypothesis_id: string | null
  goal: string | null
  steps: StepRun[]
}

export interface Event {
  id: string
  opened_at: ISODate
  machine_id: string
  anomaly: Anomaly
  state: EventState
  goal: string | null
  hypotheses: Hypothesis[]
  plan: CommanderPlan | null
  steps: StepRun[]
  approvals: string[]
  safety_actions: SafetyAction[]
  timeline: TimelineItem[]
  alert_card: AlertCard | null
  consecutive_failures: number
  replans: number
  prior_rounds?: StepRound[]   // earlier rounds' steps, oldest first (page 7 decomposition after a replan)
  closed_at: ISODate | null
  outcome: string | null
}

export interface LaborNeed {
  team: TeamId
  task: "demold" | "mold_in" | "bonding" | "a_feed" | "side_bead" | "trim"
  start: ISODate
  end: ISODate
  people: number
}

export interface ScheduleJob {
  id: string
  order_id: string
  size: TireSize
  press_id: string
  mold_id: string | null
  mold_in: ISODate
  demold: ISODate
  cure_h: number
  bond_start: ISODate
  a_feed_start: ISODate
  side_pieces: number
  bead_pieces: number
  labor: LaborNeed[]
  frozen: boolean
}

export interface StaffingChange {
  kind: "overtime" | "transfer"
  shift_date: string
  shift_name: string
  to_team: TeamId
  from_team: TeamId | null
  from_label: string | null
  people: number
  label: string
}

export interface Schedule {
  id: string
  created_at: ISODate
  horizon_start: ISODate
  horizon_end: ISODate
  jobs: ScheduleJob[]
  staffing_changes: StaffingChange[]
  solver: SolverName
  objective: number | null
  warnings: string[]
}

export interface ScheduleVersion {
  version: number
  parent_version: number | null
  status: ScheduleStatus
  created_at: ISODate
  reason: string
  event_id: string | null
  schedule: Schedule
}

export interface CapacityRowOut {
  label: string
  small: number
  medium: number
  large: number
}

export interface CapacityReport {
  rows: CapacityRowOut[]
  ml_100_weeks: [number, number]
  small_100_weeks: [number, number]
  conditions: string
}

export interface SopHit {
  sop_id: string
  title: string
  source: string
  score: number
  text: string
}

// ---------------- dashboard shapes ----------------
export interface MetricTile {
  label: "溫度" | "壓力" | "時間" | "水流" | "蒸汽"
  value: string
  status: TileStatus
}

export interface PendingAction {
  approval_id: string
  option_id: string | null
  label: string
}

export interface ApprovedAction {
  approval_id: string
  option_id: string | null
  label: string
  decided_by: string
  status: ApprovalStatus
}

export interface FocusPanel {
  event_id: string
  machine_id: string
  title: string
  detail: string
  tiles: MetricTile[]
  auto_done: string[]
  pending: PendingAction[]
  schedule_impact: string
  state: EventState
  timeline: TimelineItem[]
  approved?: ApprovedAction[]   // ✓ 已核准 (R2/R3 the supervisor approved); ✓ 已自動 is R1 only
  revoked?: string[]            // ↺ 已撤回: "從 2 線調 1 人（因主管改選夜班加班）" (a consequence, never an approval)
}

export interface OrderProgress {
  size: TireSize
  label: string
  done: number
  total: number
  remaining_h: number
  note?: string | null
}

export interface MachineTile {
  machine_id: string
  line: Line
  short_name: string
  status: MachineStatus
  temp_c: number | null
}

export interface HeaderStatus {
  normal_count: number
  abnormal_count: number
  factory_time: ISODate
  llm_mode: LLMMode
  network: "online" | "weak" | "wan_down" | "lan_down"   // page 9: weak = 弱網 (pushes delayed, agent unaffected)
  warning_light?: boolean   // page 9: LAN down -> on-site touch screen + warning light
  bad_data_total?: number   // page 10: readings the validation layer rejected (recent window), never wake the AI
  bad_data?: BadDataMark[]
}

export interface BadDataMark {
  machine_id: string
  count: number
  last_quality: DataQuality
  last_at: ISODate
}

export interface DashboardSnapshot {
  header: HeaderStatus
  orders: OrderProgress[]
  semi_finished: SemiFinishedProgress
  focus: FocusPanel | null
  machines: MachineTile[]
  events: Event[]
  pending_approvals: ApprovalRequest[]
  safety_actions: SafetyAction[]
}

export interface MachineDetail {
  state: MachineState
  tiles: MetricTile[]
  history: SensorReading[]
  prediction: Prediction | null
}

export interface SimControl {
  action: "play" | "pause" | "speed" | "reset" | "scenario"
  speed?: number | null
  scenario?: ScenarioName | null
}

export interface HealthStatus {
  ok: boolean
  llm_mode: LLMMode
  model: string
  llm_reachable: boolean
  qdrant_ok: boolean
  sim_running: boolean
  factory_time: ISODate
  uptime_s: number
}

// ---------------- the whole plant (warroom/sim/world.py, GET /api/world, WsType "world") ----------------
export type WorldOrderStatus = "待排程" | "生產中" | "完成" | "待出貨" | "已出貨"
export type WorkerState = "待命" | "在機台" | "搬運" | "休息" | "如廁" | "下班" | "請假"
export type WorldZone =
  | "war_room" | "staff" | "line_A" | "line_B" | "line_C" | "line_D" | "line_BOND" | "line_CURE"
  | "break" | "toilet" | "dock" | "off_site"
export interface WorldEvent { ts: ISODate; kind: string; text: string; ref: string | null }
export interface WorldOrder {
  id: string; size: TireSize; qty: number; done: number; due: string; customer: string; status: WorldOrderStatus
  rush: boolean; source: "seed" | "world"; created_at: ISODate | null; shipped_at: ISODate | null
}
export interface WorldMaterial {
  key: string; name: string; unit: string; stock: number; capacity: number; reorder_point: number; on_order: number
  use_per_day: number; days_left: number | null; status: "ok" | "reorder" | "low" | "out"
}
export interface PurchaseOrder {
  id: string; material: string; name: string; qty: number; unit: string; supplier: string; ordered_at: ISODate; eta: ISODate
  status: "在途" | "延遲" | "卸貨中" | "已收貨"; received_qty: number | null; note: string | null
}
export interface WorldTruck { id: string; kind: "inbound" | "outbound"; plate: string; cargo: string; arrived_at: ISODate; leaves_at: ISODate }
export interface DockEntry { ts: ISODate; kind: "arrive" | "depart"; direction: "inbound" | "outbound"; text: string }
export interface WorldWorker {
  id: string; name: string; team: TeamId; team_label: string; role: "作業員" | "組長" | "維修" | "品管" | "主管"; shift: string
  state: WorkerState; zone: WorldZone; machine_id: string | null; since: ISODate
  /** team_dataset = data/team_dataset (team's official synthetic dataset); generated = sim assumption */
  source: "team_dataset" | "generated"; note: string | null
}
export interface Inspection {
  id: string; ts: ISODate; stage: "inline" | "final"; line: string; machine_id: string; batch: string; order_id: string | null
  result: "合格" | "不良"; cause: string | null; disposition: string | null; inspector: string
}
export interface MaintenanceItem {
  id: string; machine_id: string; kind: "planned" | "repair" | "mold_change"; title: string; start: ISODate; end: ISODate | null
  status: "planned" | "in_progress" | "done"; assignee: string | null; source: "team_dataset" | "generated"
}
export interface IncidentRecord {
  id: string; date: string; machine_id: string | null; equipment: string; symptom: string; action: string
  downtime_min: number; outcome: string; source: "team_dataset"
}
export interface DailyRecord { date: string; tires_done: number; flex_util_pct: number; ot_hours: number; downtime_min: number; source: "team_dataset" }
export interface WorldSnapshot {
  factory_time: ISODate
  rev: number
  orders: { pipeline: Record<WorldOrderStatus, number>; items: WorldOrder[]; new_today: number; rush_open: number }
  materials: WorldMaterial[]
  purchase_orders: PurchaseOrder[]
  finished_goods: { by_size: Record<TireSize, number>; total: number; capacity: number; waiting_ship: number }
  dock: { trucks: WorldTruck[]; next_departure: ISODate | null; next_arrival: ISODate | null; log: DockEntry[] }
  people: { workers: WorldWorker[]; by_zone: Partial<Record<WorldZone, number>>; by_state: Partial<Record<WorkerState, number>>; on_duty: number; total: number }
  energy: { total_kw: number; by_line: Record<string, number>; base_kw: number; contract_kw: number; peak: boolean; kwh_today: number; history: [ISODate, number][] }
  quality: { inspected_today: number; defects_today: number; yield_pct: number | null; recent: Inspection[]; incidents: IncidentRecord[]; daily: DailyRecord[] }
  maintenance: { in_progress: MaintenanceItem[]; upcoming: MaintenanceItem[]; recent: MaintenanceItem[] }
  events: WorldEvent[]
}
/** WS "world": full = every section; otherwise only the sections that changed since the last message. */
export type WorldDelta = { full: boolean; rev: number; factory_time: ISODate } & Partial<Omit<WorldSnapshot, "factory_time" | "rev">>

// ---------------- WebSocket ----------------
export type WsMessage =
  | { type: "snapshot"; ts: ISODate; data: DashboardSnapshot }
  | { type: "machines"; ts: ISODate; data: { machines: MachineTile[] } }
  | { type: "anomaly"; ts: ISODate; data: Anomaly }
  | { type: "safety_action"; ts: ISODate; data: SafetyAction }
  | { type: "event"; ts: ISODate; data: Event }
  | { type: "alert_card"; ts: ISODate; data: { event_id: string; card: AlertCard; source?: "detector" | "commander" } }
  | { type: "approval_request"; ts: ISODate; data: ApprovalRequest }
  | { type: "approval_resolved"; ts: ISODate; data: ApprovalRequest }
  | { type: "schedule"; ts: ISODate; data: ScheduleVersion }
  | { type: "audit"; ts: ISODate; data: AuditEntry }
  | { type: "clock"; ts: ISODate; data: { factory_time: ISODate; speed: number; paused: boolean } }
  | { type: "world"; ts: ISODate; data: WorldDelta }

// ---------------- REST ----------------
export const API = {
  health: "/api/health",
  snapshot: "/api/snapshot",
  machines: "/api/machines",
  machine: "/api/machines/{machine_id}",
  events: "/api/events",
  event: "/api/events/{event_id}",
  approvals: "/api/approvals", // ?status=pending
  approvalDecision: "/api/approvals/{request_id}/decision", // POST ApprovalDecision -> ApprovalRequest
  schedule: "/api/schedule",
  scheduleVersions: "/api/schedule/versions",
  scheduleRollback: "/api/schedule/rollback", // POST {version?: number}
  capacity: "/api/capacity",
  orders: "/api/orders",
  shifts: "/api/shifts",
  audit: "/api/audit", // ?since_seq=N&limit=200
  sopSearch: "/api/sop/search", // ?q=...&k=3
  simControl: "/api/sim/control", // POST SimControl
  ask: "/api/ask", // POST AskRequest -> AskAnswer (問戰情室, read-only)
  world: "/api/world", // GET WorldSnapshot: the whole plant (issue #11)
  ws: "/ws",
} as const

export function apiPath(template: string, params: Record<string, string> = {}): string {
  return template.replace(/\{(\w+)\}/g, (_, k: string) => encodeURIComponent(params[k] ?? ""))
}
