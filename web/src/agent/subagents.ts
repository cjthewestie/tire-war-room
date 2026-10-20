// Words for the main agent's subagents (warroom/agent/orchestrator.py), shared by 主控台, the Agent page and 狀態機.
import type { SubagentStatus } from '../contracts'

export const SUB_STATUS: Record<SubagentStatus, string> = {
  queued: '排隊中', running: '執行中', waiting_model: '等模型', waiting_approval: '等主管核准', waiting_tasks: '等人員完成',
  done: '完成', failed: '安全中止', interrupted: '已中斷',
}
export const SUB_TONE: Record<SubagentStatus, 'live' | 'wait' | 'done' | 'bad'> = {
  queued: 'wait', running: 'live', waiting_model: 'wait', waiting_approval: 'wait', waiting_tasks: 'live', done: 'done', failed: 'bad', interrupted: 'bad',
}
