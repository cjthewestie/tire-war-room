// 現場處置 SOP (#72, #73): which SOP fits a machine's open event, and its 處置步驟 as a list.
// Read-only: the SOP is the one the event's sop_search step found, else GET /api/sop/search; its steps come from the
// same SOP markdown the knowledge base indexes (warroom/kb/sop/*.md, bundled at build time), because the search
// returns one chunk per SOP and a long 處置步驟 section is split over several chunks.
import { API, type Event, type SopHit } from './contracts'
import { machineLabel } from './factory'

const SOP_FILES = import.meta.glob('../../warroom/kb/sop/*.md', { query: '?raw', import: 'default', eager: true }) as Record<string, string>
const SOP_TEXT: Record<string, string> = Object.fromEntries(Object.values(SOP_FILES).flatMap((t) => {
  const m = t.split('\n', 1)[0].match(/^#\s*(SOP-[A-Z]+-\d+)\s/)
  return m ? [[m[1], t]] : []
}))

/** The 處置步驟 section of a house SOP, from the bundled markdown. */
export function sopStepsFromFile(sopId: string): string[] | null {
  const text = SOP_TEXT[sopId]
  if (!text) return null
  const sec = text.split(/\n(?=## )/).find((b) => /^## [^\n]*處置步驟/.test(b))
  return sec ? parseSteps(sec) : null
}

export interface SopSteps { sop_id: string; title: string; steps: string[] }

const TERMINAL = new Set(['closed', 'safe_stop'])

/** The newest open event on this machine (plant events use machine_id "PLANT" and are skipped). */
export function openEventFor(events: Event[] | undefined, machineId: string): Event | null {
  return [...(events ?? [])]
    .filter((e) => e.machine_id === machineId && !TERMINAL.has(e.state))
    .sort((a, b) => (a.opened_at < b.opened_at ? 1 : -1))[0] ?? null
}

/** The SOP the event's own sop_search step picked, if it ran. */
function sopFromEvent(ev: Event | null): { sop_id: string; title: string } | null {
  for (const sr of [...(ev?.steps ?? [])].reverse()) {
    if (sr.step.tool !== 'sop_search' || !sr.result?.ok) continue
    const hits = sr.result.data.hits as { sop_id?: string; title?: string }[] | undefined
    const h = hits?.find((x) => x.sop_id && x.title)
    if (h) return { sop_id: h.sop_id!, title: h.title! }
  }
  return null
}

type Reading = { label: string; value: string; status: string }

/** What to search for: the event's alert, else the machine's abnormal readings. */
export function sopQuery(machineId: string, ev: Event | null, readings: Reading[] = []): string {
  if (ev) return `${machineLabel(machineId)} ${ev.alert_card?.title ?? ev.anomaly.message}`
  const bad = readings.filter((t) => t.status !== 'normal').map((t) => `${t.label}${t.value}`)
  return `${machineLabel(machineId)} ${bad.join(' ') || '異常'}`
}

async function search(q: string, k: number): Promise<SopHit[]> {
  const r = await fetch(`${API.sopSearch}?q=${encodeURIComponent(q)}&k=${k}`)
  if (!r.ok) throw new Error(`${r.status} SOP 查詢失敗`)
  return (await r.json()) as SopHit[]
}

/** The first line is a heading (## … or 〔SOP-ID 標題｜section〕); numbered lines are steps, indented lines belong
 *  to the step above. */
export function parseSteps(text: string): string[] {
  const out: string[] = []
  for (const raw of text.split('\n').slice(1)) {
    const line = raw.trimEnd()
    if (!line.trim()) continue
    const m = line.match(/^(\d+)[.、)]\s*(.*)$/)
    if (m) out.push(m[2])
    else if (out.length) out[out.length - 1] += `\n${line.trim()}`
  }
  return out
}

/** Find the SOP for a machine's anomaly and return its 處置步驟. */
export async function loadSopSteps(machineId: string, ev: Event | null, readings: Reading[] = []): Promise<SopSteps | null> {
  const pick = sopFromEvent(ev) ?? (await search(sopQuery(machineId, ev, readings), 3).then((h) => h[0] ?? null))
  if (!pick) return null
  const fromFile = sopStepsFromFile(pick.sop_id)
  if (fromFile) return { sop_id: pick.sop_id, title: pick.title, steps: fromFile }
  // team-dataset SOPs are not bundled: use the chunk the search returns
  const hits = await search(`${pick.title} 處置步驟`, 8)
  const chunk = hits.find((h) => h.sop_id === pick.sop_id && /｜[^〕]*處置步驟/.test(h.text.split('\n')[0] ?? ''))
  return { sop_id: pick.sop_id, title: pick.title, steps: chunk ? parseSteps(chunk.text) : [] }
}
