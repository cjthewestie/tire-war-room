// The guided demo's shared cue state (warroom/api/demo.py, GET/POST /api/demo/state, WS "demo"). One pointer that the
// 3D factory, the war room and the phone all follow; whoever presses continue / pause / exit posts it here.

export type DemoWaiting = 'continue' | 'approval' | 'event' | null

export interface DemoState {
  running: boolean
  cue_id: string | null
  cue_index: number
  started_at: string | null
  paused: boolean
  /** what the current beat waits for before the next: 按 A 繼續, the real approval, or a plant event */
  waiting_for: DemoWaiting
  story_target_s: number | null
  elapsed_s: number
  /** the screen that runs the timeline: '3d' or 'warroom' */
  driver: string | null
  cues: string[]
  rev: number
}

export interface DemoCommand {
  action: 'start' | 'continue' | 'pause' | 'resume' | 'exit' | 'cue'
  cue_id?: string
  cue_index?: number
  waiting_for?: DemoWaiting
  driver?: string
  cues?: string[]
  story_target_s?: number
}

export const DEMO_API = '/api/demo/state'
