/** #console?event=<id>: the event the Agent page opens on (kept out of the lazy page chunk). */
export function readConsoleEvent(): string | null {
  const m = /[?&]event=([^&]+)/.exec(location.hash)
  return m ? decodeURIComponent(m[1]) : null
}
