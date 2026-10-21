// QA 模式 live proofs and the 「看一次怎麼運作」 walkthrough: types, API calls and which proof backs which question.
// Server: warroom/api/qa_demo.py (GET /api/qa/proofs, POST /api/qa/proofs/{id}/run, POST /api/qa/walk, GET /api/qa/walk/{id}).
import { apiGet, supervisorPost } from '../transport'

export interface Proof {
  id: string; title: string; doubt: string; supports: string[]; steps: string[]; expected: string; cleanup: string
  touches_plant: boolean; page: string; status: 'ready' | 'pending'
}
export interface WalkKind { id: string; title: string; line: string; path: string; how: string; arg: string | null; status: string }
export interface Registry { proofs: Proof[]; walks: WalkKind[]; busy: boolean; open_events: string[]; pending_approvals: string[] }
export interface ProofStep { label: string; ok: boolean; detail: unknown }
export interface ProofResult { id: string; title: string; status: 'ok' | 'failed' | 'pending' | 'error'; page: string; steps: ProofStep[]; ms: number; error?: string; data: Record<string, unknown> }
export interface Check { what: string; done: boolean; at: string | null }
export interface TrackItem { page: string; label: string; href: string; external: boolean; checks: Check[] }
export interface StateStep { state: string; at: string; seconds: number; llm_calls: number }
export interface WalkView {
  id: string; kind: string; title: string; path: string; event_id: string | null; events: string[]; machine_id: string | null
  states: StateStep[]; stages_reached: string[]; tracker: TrackItem[]; waiting_approval: string[]; decided: string[]
  paused: boolean; speed: number | null; paused_for: string | null; closed: boolean; factory_time: string; audit_rows: number
}

export const getRegistry = () => apiGet<Registry>('/api/qa/proofs')
export const runProof = (id: string) => supervisorPost<ProofResult>(`/api/qa/proofs/${encodeURIComponent(id)}/run`, {})
export const startWalk = (kind: string, speed: number, pauseAtApproval: boolean) =>
  supervisorPost<WalkView>('/api/qa/walk', { kind, speed, pause_at_approval: pauseAtApproval })
export const getWalk = (id: string) => apiGet<WalkView>(`/api/qa/walk/${encodeURIComponent(id)}`)
export const walkControl = (id: string, action: 'play' | 'pause' | 'speed', speed?: number) =>
  supervisorPost<WalkView>(`/api/qa/walk/${encodeURIComponent(id)}/control`, { action, speed })

/** Which live proof backs an answer: matched on the judge's question. */
const MATCH: [string, RegExp][] = [
  ['no_rogue_write', /亂改|自己(去)?動|下指令|控制機台|改產線|失控/],
  ['offline', /斷網|離線|網路|offline/i],
  ['safety_core', /安全核心|safety core|停機/i],
  ['llm_only_plan', /叫 ?AI|呼叫.*(AI|LLM|模型)|哪裡.*(模型|LLM)|只有.*(PLAN|AI)/i],
  ['hypothesis', /計[劃畫].*(有效|對)|驗證|猜錯|假設|推翻/],
  ['audit_tamper', /稽核|竄改|log|紀錄.*(改|刪)/i],
  ['injection', /injection|注入|惡意指令/i],
  ['tools', /工具|模組化|白名單|bash|命令列/i],
  ['model', /模型|多快|Nemotron|NVFP4|DGX/i],
  ['rag_no_answer', /亂答|知識庫|RAG|沒有的/i],
]
export function proofsFor(question: string): string[] {
  return MATCH.filter(([, rx]) => rx.test(question)).map(([id]) => id).slice(0, 2)
}

export const STATE_LABEL: Record<string, string> = {
  detected: '偵測', classify: '分類', plan: '計劃', simulate: '模擬', gate: '把關', await_approval: '等核准',
  execute: '執行', verify: '驗證', replan: '重新計劃', closed: '結案', safe_stop: '安全停止',
}

export function hms(iso: string | null): string {
  if (!iso) return ''
  const m = /T(\d{2}:\d{2}:\d{2})/.exec(iso)
  return m ? m[1] : iso
}
