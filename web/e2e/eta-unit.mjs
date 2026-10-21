// Unit checks (no server, no browser) for the phone's 修復預估 under fast repair (GET /api/health repair_fast_s > 0,
// Kyle 10-21): 「多久修好：約 16 秒（維修 12 秒＋回溫 4 秒）」, 預計完成 「約 N 秒後」 in wall seconds, a repair task row
// 「約 N 秒」 from its progress — and that number never goes back up when the presenter switches 10× ↔ 60× mid-repair.
// Old servers (no repair_fast_s, or 0; no recover_fast_s) keep the window wording / show no 回溫 part.
// Run: cd web && node e2e/eta-unit.mjs
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const here = dirname(fileURLToPath(import.meta.url))
let failed = 0
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }

const vite = await createServer({ root: resolve(here, '..'), configFile: false, logLevel: 'error', appType: 'custom',
  server: { middlewareMode: true, hmr: false, ws: false } })
const { fastApplies, fastDoneText, fastRepairOf, fastWindowText, isRepairTask, secText, taskLeftS } =
  await vite.ssrLoadModule('/src/phone/fastEta.ts')

// ---------------------------------------------------------------- health → settings
const F = fastRepairOf({ repair_fast_s: 12, recover_fast_s: 4 })
check('health 12 + 4 → {12, 4}', F?.repair_s === 12 && F?.recover_s === 4, JSON.stringify(F))
const OLD = fastRepairOf({ repair_fast_s: 3 })
check('current base (repair_fast_s 3, no recover_fast_s) → {3, 0}', OLD?.repair_s === 3 && OLD?.recover_s === 0, JSON.stringify(OLD))
check('repair_fast_s 0 → null (window wording)', fastRepairOf({ repair_fast_s: 0, recover_fast_s: 4 }) === null)
check('older server without the field → null', fastRepairOf({ ok: true }) === null && fastRepairOf(null) === null)

// ---------------------------------------------------------------- 多久修好
check('多久修好 12 + 4', fastWindowText(F) === '約 16 秒（維修 12 秒＋回溫 4 秒）', fastWindowText(F))
check('多久修好 3 + 0 (no 回溫 part)', fastWindowText(OLD) === '約 3 秒（維修 3 秒）', fastWindowText(OLD))
check('多久修好 env values (REPAIR_FAST_S 8, RECOVER_FAST_S 2.5)',
  fastWindowText(fastRepairOf({ repair_fast_s: 8, recover_fast_s: 2.5 })) === '約 10.5 秒（維修 8 秒＋回溫 2.5 秒）')

// ---------------------------------------------------------------- 預計完成
const task = (status, progress, kind = '檢修') => ({ kind, status, progress })
const done = (r, decided, repaired) => { const d = fastDoneText(r, F, decided, repaired); return d.lead + d.text }
check('card waiting, 檢修 held 待派 → 現在核准的話，約 16 秒後', done([task('待派', 0)], false) === '現在核准的話，約 16 秒後', done([task('待派', 0)], false))
check('card waiting, no repair task yet (R2 檢修 in the card) → 現在核准的話，約 16 秒後', done([], false) === '現在核准的話，約 16 秒後')
check('approved, task not taken yet → 約 16 秒後', done([task('待派', 0)], true) === '約 16 秒後')
check('前往中 25% → 9 s repair + 4 s 回溫 = 約 13 秒後', done([task('前往中', 0.25)], true) === '約 13 秒後', done([task('前往中', 0.25)], true))
check('two repair tasks → the slowest one counts', done([task('執行中', 0.9), task('執行中', 0.5)], true) === '約 10 秒後')
check('done tasks do not count while another is open', done([task('完成', 1), task('執行中', 0.75)], true) === '約 7 秒後')
check('every repair done → 回溫中', done([task('完成', 1)], true) === '維修已完成，回溫中（約 4 秒）', done([task('完成', 1)], true))
check('Event.repaired_at (tasks trimmed) → 回溫中', done([], true, true) === '維修已完成，回溫中（約 4 秒）')
check('every repair done, no 回溫 on the server → 確認恢復中',
  fastDoneText([task('完成', 1)], OLD, true).text === '維修已完成，確認恢復中')
check('R1-only event (no card) already running → no 現在核准的話', fastDoneText([task('執行中', 0.5)], F, false).lead === '')

// ---------------------------------------------------------------- task rows
check('task row 待派 → 約 12 秒', secText(taskLeftS(task('待派', 0), F)) === '約 12 秒')
check('task row 執行中 80% → 約 3 秒 (2.4 s rounded up)', secText(taskLeftS(task('執行中', 0.8), F)) === '約 3 秒')
check('task row almost done → never 約 0 秒', secText(taskLeftS(task('執行中', 0.999), F)) === '約 1 秒')
check('task row 75% → 約 3 秒 (float 3.0000000000000004 stays 3)', secText(taskLeftS(task('執行中', 0.75), F)) === '約 3 秒')
check('done task → 0 s left', taskLeftS(task('完成', 1), F) === 0)
check('repair kinds: 檢修／停用檢修／放慢投料／暫停投料, not 宣達／調派支援',
  ['檢修', '停用檢修', '放慢投料', '暫停投料'].every((k) => isRepairTask({ kind: k })) && !isRepairTask({ kind: '宣達' }) && !isRepairTask({ kind: '調派支援' }))

// ---------------------------------------------------------------- speed switch mid-repair (10× → 60× → 10×)
// The server (warroom/sim/work.py): progress = wall seconds run / (fast_s − 0.25); the factory eta was set at creation:
// created_at + fast_s × speed₀ / 60 min. Our number comes from progress; the old one from (eta − factory now).
{
  const FAST_SLACK_S = 0.25
  const t0F = Date.parse('2026-10-22T09:00:00+08:00')
  let wall = 0, nowF = t0F, speed = 10, ran = 0
  const etaF = t0F + F.repair_s * speed * 1000          // 12 wall s at 10× = 120 factory s
  const ours = [], viaEta = []
  while (ran < F.repair_s - FAST_SLACK_S) {
    const dt = 0.2
    wall += dt; ran += dt; nowF += dt * speed * 1000
    if (wall > 4 && wall <= 4.2) speed = 60               // presenter: 60×
    if (wall > 8 && wall <= 8.2) speed = 10               // … and back to 10×
    const p = Math.min(1, ran / (F.repair_s - FAST_SLACK_S))
    if (p >= 1) break
    const t = task(p < 1 / 3 ? '前往中' : '執行中', Math.round(p * 1000) / 1000)
    ours.push(Number(/約 (\d+) 秒/.exec(fastDoneText([t], F, true).text)[1]))
    viaEta.push(Math.round((etaF - nowF) / 1000 / speed))  // factory seconds left ÷ current speed
  }
  const nonIncreasing = (a) => a.every((v, i) => i === 0 || v <= a[i - 1])
  check('speed switch: 預計完成 seconds never go back up', nonIncreasing(ours), `${ours[0]} → ${ours.at(-1)} (${ours.length} turns)`)
  check('speed switch: starts at 16 (12 + 4) and ends near the 4 s 回溫', ours[0] === 16 && ours.at(-1) <= 5, `${ours[0]} … ${ours.at(-1)}`)
  check('… whereas the factory-eta way would jump (why progress is used)', !nonIncreasing(viaEta) || viaEta.some((v) => v < 0),
    `eta-based: min ${Math.min(...viaEta)}, max ${Math.max(...viaEta)}`)
  const rows = ours.map((v) => v - F.recover_s)
  check('speed switch: task row seconds never go back up either', nonIncreasing(rows), `${rows[0]} → ${rows.at(-1)}`)
}

// ---------------------------------------------------------------- which events get the fast wording
const step = (tool, kind) => ({ id: 's', tool, args: kind ? { kind, machine_id: 'CURE-07', teams: [] } : {}, depends_on: [], purpose: '' })
const evOf = (source, steps = [], planSteps = []) => ({ source, steps: steps.map((s) => ({ step: s })), plan: { steps: planSteps } })
check('machine anomaly with a 檢修 task → fast', fastApplies(evOf('detector'), [{ kind: '檢修' }]))
check('machine anomaly, card holds notify_teams maintenance → fast', fastApplies(evOf('detector', [], [step('notify_teams', 'maintenance')]), []))
check('machine anomaly, R3 machine_out step → fast', fastApplies(evOf(undefined, [step('notify_teams', 'machine_out')]), []))
check('schedule-only plan (confirm_schedule, 宣達) → window wording', !fastApplies(evOf('detector', [step('confirm_schedule'), step('notify_teams', 'info')]), [{ kind: '宣達' }]))
check('plant event → window wording', !fastApplies(evOf('plant'), [{ kind: '檢修' }]))
check('supervisor command → window wording', !fastApplies(evOf('supervisor', [], [step('notify_teams', 'maintenance')]), []))

await vite.close()
console.log(failed ? `${failed} FAILED` : 'ALL PASS')
process.exit(failed ? 1 : 0)
