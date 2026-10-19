// Split-flap timing and the change counter behind <Flap> (see Flap.tsx, DESIGN.md).
import { useState } from 'react'

export const FLAP_STEP_MS = 50

/** Counts how many times `sig` has changed since mount (0 = never), so a row can flip all its cells at once. */
export function useFlipGen(sig: string): number {
  // React's "state from the previous render" pattern: no effect, no extra frame before the flip starts
  const [st, setSt] = useState({ sig, gen: 0 })
  if (st.sig !== sig) {
    setSt({ sig, gen: st.gen + 1 })
    return st.gen + 1
  }
  return st.gen
}
