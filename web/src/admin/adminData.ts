// 後台 shared data bits: the admin GET hook, the reindex job shape and the audit-chain verify result.
import { useCallback, useEffect, useState } from 'react'
import { adminFetch } from '../transport'

export interface Job { state: 'idle' | 'running' | 'done' | 'failed'; phase: string; files: number; chunks: number | null; team_chunks: number | null; mode: string | null; error: string | null; elapsed_s: number | null }

export interface Verify { ok: boolean; checked: number | null; last_seq: number | null; broken_at: number | null; problem: string | null }
export function verifyText(v: Verify): string {
  return v.ok ? `✓ 完整${v.checked != null ? `（驗了 ${v.checked} 筆）` : ''}` : `✗ 第 ${v.broken_at ?? '?'} 筆斷裂`
}

export function useLoad<T>(path: string): { data: T | null; err: string | null; reload: () => void } {
  const [data, setData] = useState<T | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [n, setN] = useState(0)
  useEffect(() => {
    let off = false
    adminFetch<T>(path).then((d) => { if (!off) { setData(d); setErr(null) } }).catch((e) => { if (!off) setErr(String(e).replace(/^Error: /, '')) })
    return () => { off = true }
  }, [path, n])
  return { data, err, reload: useCallback(() => setN((x) => x + 1), []) }
}
