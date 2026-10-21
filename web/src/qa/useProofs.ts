// The live proofs' registry, results and the one proof running now (shared by the index and the answer card).
import { useCallback, useEffect, useState } from 'react'
import { getRegistry, runProof, type Proof, type ProofResult, type Registry } from './qaDemo'

export function useProofs() {
  const [reg, setReg] = useState<Registry | null>(null)
  const [results, setResults] = useState<Record<string, ProofResult>>({})
  const [running, setRunning] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const refresh = useCallback(() => getRegistry().then(setReg).catch((e) => setErr(String(e))), [])
  useEffect(() => { void refresh() }, [refresh])
  const run = useCallback(async (p: Proof) => {
    if (running) return
    const fresh = await getRegistry().catch(() => reg)
    if (fresh) setReg(fresh)
    if (p.touches_plant && fresh?.busy &&
      !window.confirm(`現在有 ${fresh.open_events.length} 件處理中、${fresh.pending_approvals.length} 張待核准。「${p.title}」會在模擬裡開一個事件（標記「QA 展示」，不會動到核准）。要繼續嗎？`)) return
    setRunning(p.id); setErr(null)
    try {
      const r = await runProof(p.id)
      setResults((x) => ({ ...x, [p.id]: r }))
    } catch (e) {
      setErr(`「${p.title}」沒有跑完：${String(e instanceof Error ? e.message : e)}`)
    } finally {
      setRunning(null)
    }
  }, [reg, running])
  return { reg, results, running, err, run }
}
export type ProofsApi = ReturnType<typeof useProofs>
