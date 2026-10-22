// Unit checks (no browser) for the Xbox controller (src/factory3d/gamepad.ts) and the demo's input lock
// (src/factory3d/demoInput.ts), with a fake Gamepad object: dead zone, button edges and holds, the standard mapping,
// free inputs ignored during the demo and working again after it, and the keyboard and the pad giving the same actions.
// Run: cd web && node e2e/gamepad-unit.mjs
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const here = dirname(fileURLToPath(import.meta.url))
let failed = 0
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }
const vite = await createServer({ root: resolve(here, '..'), configFile: false, logLevel: 'error', appType: 'custom',
  server: { middlewareMode: true, hmr: false, ws: false } })
const G = await vite.ssrLoadModule('/src/factory3d/gamepad.ts')
const I = await vite.ssrLoadModule('/src/factory3d/demoInput.ts')
await vite.close()

/** A fake standard Gamepad: axes [lx, ly, rx, ry], buttons by name. */
function pad({ axes = [0, 0, 0, 0], down = [], lt = 0, rt = 0 } = {}) {
  const buttons = Array.from({ length: 17 }, () => ({ pressed: false, value: 0 }))
  for (const n of down) buttons[G.BTN[n]] = { pressed: true, value: 1 }
  buttons[G.BTN.LT] = { pressed: lt > 0.5, value: lt }
  buttons[G.BTN.RT] = { pressed: rt > 0.5, value: rt }
  return { axes, buttons, mapping: 'standard', connected: true, id: 'Xbox Wireless Controller (STANDARD GAMEPAD)' }
}

// dead zone
check('dead zone: 0.14 → 0', G.dead(0.14) === 0 && G.dead(-0.149) === 0)
check('dead zone: rescaled, no jump at the edge', Math.abs(G.dead(0.16)) < 0.02 && G.dead(1) === 1 && G.dead(-1) === -1)
check('dead zone: NaN → 0', G.dead(NaN) === 0)
const [sx, sy] = G.deadStick(0.1, 0.1)
check('radial dead zone: a small diagonal stays 0', sx === 0 && sy === 0)
const [dx, dy] = G.deadStick(0.6, 0.6)
check('radial dead zone: a diagonal keeps its direction', Math.abs(dx - dy) < 1e-9 && dx > 0.5)
const f0 = G.readPad(pad({ axes: [0.05, -0.1, 0.9, 0], lt: 0.04, rt: 0.8 }))
check('readPad: drift inside the dead zone is 0', f0.lx === 0 && f0.ly === 0 && f0.rx > 0.8)
check('readPad: triggers', f0.lt === 0 && f0.rt > 0.7)

// edges and holds
const a = G.readPad(pad({ down: ['A'] })).down, none = G.readPad(pad()).down
check('edge: A pressed once', G.pressed(none, a).join() === 'A')
check('edge: holding A is not a second press', G.pressed(a, a).length === 0)
check('edge: release', G.released(a, none).join() === 'A')
check('edge: first frame counts', G.pressed(null, a).join() === 'A')
let held = {}
for (let i = 0; i < 12; i++) held = G.holds(held, G.readPad(pad({ down: ['B'] })).down, 0.1)
check('hold: B 1.2 s', Math.abs(held[G.BTN.B] - 1.2) < 1e-9)
held = G.holds(held, none, 0.1)
check('hold: resets on release', held[G.BTN.B] === undefined)
check('smoothing eases, never overshoots', G.approach(0, 1, 0.016, 8) > 0 && G.approach(0, 1, 0.016, 8) < 0.2 && G.approach(0, 1, 1, 8) === 1)

// mapping (Xbox names in the help)
const help = G.PAD_HELP.map((r) => r[0]).join(' ')
check('help lists Xbox names', ['A', 'B', 'X', 'Y', 'LB／RB', 'LT／RT', 'Menu', 'View'].every((n) => help.includes(n)), help)

// the demo's input lock
const ctx = (o = {}) => ({ demoOn: false, confirmExit: false, slides: false, slideView: false, phoneUp: false, ...o })
const run = (p, edges, c, h = {}) => I.padActions(G.readPad(pad(p)), edges, h, c, false)
const walk = { axes: [0.8, -0.9, 0.7, 0.4], lt: 0.9 }
const free = run(walk, ['Right', 'A', 'Y'], ctx())
check('free: sticks move and look, triggers zoom', free.move[0] > 0.6 && free.move[1] > 0.5 && free.look[0] > 0.6 && free.zoom > 0.8)
check('free: D-pad focus, A open, Y fly', ['focus+', 'open', 'fly'].every((x) => free.actions.includes(x)), free.actions.join())
const demo = run(walk, ['Right', 'A', 'Y', 'Menu'], ctx({ demoOn: true }))
check('demo: sticks, triggers and D-pad ignored', demo.move.every((v) => v === 0) && demo.look.every((v) => v === 0) && demo.zoom === 0 && !demo.actions.includes('focus+') && !demo.actions.includes('fly'), JSON.stringify(demo))
check('demo: A continue, Menu pause', demo.actions.includes('continue') && demo.actions.includes('pause'))
check('demo: B tap does not exit', !run({}, ['B'], ctx({ demoOn: true })).actions.includes('exit-ask'))
check('demo: B held 1 s asks 「離開 Demo？」', run({}, [], ctx({ demoOn: true }), { [G.BTN.B]: 1.05 }).actions.includes('exit-ask'))
check('demo: confirm with A, cancel with B', run({}, ['A'], ctx({ demoOn: true, confirmExit: true })).actions.join() === 'exit-yes' && run({}, ['B'], ctx({ demoOn: true, confirmExit: true })).actions.join() === 'exit-no')
check('demo: LB / RB only at the board', run({}, ['RB'], ctx({ demoOn: true, slides: true })).actions.includes('slide+') && !run({}, ['RB'], ctx({ demoOn: true })).actions.includes('slide+'))
const after = run(walk, ['Right'], ctx({ demoOn: false }))
check('after exit: free inputs work again', after.move[0] > 0.6 && after.actions.includes('focus+'))
check('freeInput(): off in the demo, on after', I.freeInput({ demoOn: true }) === false && I.freeInput({ demoOn: false }) === true)

// keyboard ↔ pad: the same actions
check('keys: G starts (not in a demo)', I.demoKey('KeyG', ctx()) === 'start' && I.demoKey('KeyG', ctx({ demoOn: true })) === null)
check('keys: Space / N continue (= A)', I.demoKey('Space', ctx({ demoOn: true })) === 'continue' && I.demoKey('KeyN', ctx({ demoOn: true })) === 'continue')
check('keys: B pause (= Menu)', I.demoKey('KeyB', ctx({ demoOn: true })) === 'pause')
check('keys: Esc asks to exit (= hold B), Enter confirms', I.demoKey('Escape', ctx({ demoOn: true })) === 'exit-ask' && I.demoKey('Enter', ctx({ demoOn: true, confirmExit: true })) === 'exit-yes')
check('keys: ← → slides at the board (= LB RB)', I.demoKey('ArrowRight', ctx({ demoOn: true, slides: true })) === 'slide+' && I.demoKey('ArrowRight', ctx({ demoOn: true })) === null)
check('keys: WASD do nothing in the demo', ['KeyW', 'KeyA', 'KeyS', 'KeyD', 'KeyV', 'KeyF', 'KeyT', 'Digit1'].every((k) => I.demoKey(k, ctx({ demoOn: true })) === null))

console.log(failed ? `\n${failed} FAILED` : '\nall passed')
process.exit(failed ? 1 : 0)
