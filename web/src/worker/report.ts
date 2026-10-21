// 人員回報 (CJ, 2026-10-22: 「加回報」): /worker posts 到達・開始 and 完成 to POST /api/tasks/{id}/start|done
// (warroom/api/tasks.py). On the on-site tablet no PIN is needed; a phone sends the worker's own PIN, asked once
// per tab and kept in sessionStorage. A photo is shrunk on the phone to a JPEG well under the server's 2 MB cap.
const PIN_KEY = 'worker-pin'
const MAX_SIDE = 1280

function pin(): string {
  try { return sessionStorage.getItem(PIN_KEY) ?? '' } catch { return '' }
}

export class ReportError extends Error {
  status: number
  constructor(status: number, message: string) { super(message); this.status = status }
}

export async function report(taskId: string, action: 'start' | 'done',
  body: { worker_id: string; note?: string; photo?: string }, askPin: () => string | null, retried = false): Promise<void> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const p = pin()
  if (p) headers['X-Worker-Pin'] = p
  const r = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/${action}`, { method: 'POST', headers, body: JSON.stringify(body) })
  if (r.ok) return
  let detail = ''
  try { detail = String((await r.json()).detail ?? '') } catch { /* not JSON */ }
  if (r.status === 401 && !retried) {
    try { sessionStorage.removeItem(PIN_KEY) } catch { /* private mode */ }
    const got = askPin()
    if (got) {
      try { sessionStorage.setItem(PIN_KEY, got) } catch { /* private mode: asked again next time */ }
      return report(taskId, action, body, askPin, true)
    }
  }
  throw new ReportError(r.status, detail || `${r.status}`)
}

/** A camera photo as a JPEG data URL, longest side MAX_SIDE px. */
export async function shrinkPhoto(file: File): Promise<string> {
  const url = URL.createObjectURL(file)
  try {
    const img = await new Promise<HTMLImageElement>((ok, bad) => {
      const i = new Image()
      i.onload = () => ok(i)
      i.onerror = () => bad(new Error('not an image'))
      i.src = url
    })
    const k = Math.min(1, MAX_SIDE / Math.max(img.naturalWidth, img.naturalHeight))
    const c = document.createElement('canvas')
    c.width = Math.max(1, Math.round(img.naturalWidth * k))
    c.height = Math.max(1, Math.round(img.naturalHeight * k))
    c.getContext('2d')?.drawImage(img, 0, 0, c.width, c.height)
    return c.toDataURL('image/jpeg', 0.8)
  } finally {
    URL.revokeObjectURL(url)
  }
}
