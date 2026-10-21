// CJ's Xbox pad drives the demo on both screens (standard mapping): A = continue, Menu (☰, button 9) = pause / resume.
// Pure edge detection here (tested in e2e/demo-warroom-unit.mjs), the polling hook in useDemo.ts.

export const PAD_A = 0
export const PAD_MENU = 9

type PadLike = { buttons: ReadonlyArray<{ pressed: boolean }> } | null

/** Buttons held now on any connected pad, and the ones newly pressed since `prev` (a press fires once, not while held). */
export function padEdges(prev: ReadonlySet<number>, pads: ReadonlyArray<PadLike>): { held: Set<number>; pressed: number[] } {
  const held = new Set<number>()
  for (const p of pads) p?.buttons.forEach((b, i) => { if (b.pressed) held.add(i) })
  return { held, pressed: [...held].filter((i) => !prev.has(i)) }
}
