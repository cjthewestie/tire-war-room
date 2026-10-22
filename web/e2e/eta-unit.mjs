// Unit checks (no server, no browser) for the phone's 修復預估 under fast repair (GET /api/health repair_fast_s > 0,
// Kyle 10-21): 「多久修好：約 16 秒（維修 12 秒＋回溫 4 秒）」, 預計完成 「約 N 秒後」 in wall seconds (shown once approved,
// veteran 10-22), a repair task row 「約 N 秒」 from its progress — and that number never goes back up when the presenter
// switches 10× ↔ 60× mid-repair. Old servers (no repair_fast_s, or 0; no recover_fast_s) keep the window wording / show
// just 「約 3 秒」. Then the phone card's words (cardText.ts, optionGuide.optionChoices): title, sentence, 核准後會做, 方案,
// the one line per event while watching.
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
check('多久修好 3 + 0 (no 回溫: just 約 3 秒)', fastWindowText(OLD) === '約 3 秒', fastWindowText(OLD))
check('多久修好 env values (REPAIR_FAST_S 8, RECOVER_FAST_S 2.5)',
  fastWindowText(fastRepairOf({ repair_fast_s: 8, recover_fast_s: 2.5 })) === '約 10.5 秒（維修 8 秒＋回溫 2.5 秒）')

// ---------------------------------------------------------------- 預計完成
const task = (status, progress, kind = '檢修') => ({ kind, status, progress })
const done = (r, repaired) => fastDoneText(r, F, repaired)
check('approved, task not taken yet → 約 16 秒後', done([task('待派', 0)]) === '約 16 秒後', done([task('待派', 0)]))
check('approved, no repair task yet → 約 16 秒後', done([]) === '約 16 秒後')
check('前往中 25% → 9 s repair + 4 s 回溫 = 約 13 秒後', done([task('前往中', 0.25)]) === '約 13 秒後', done([task('前往中', 0.25)]))
check('two repair tasks → the slowest one counts', done([task('執行中', 0.9), task('執行中', 0.5)]) === '約 10 秒後')
check('done tasks do not count while another is open', done([task('完成', 1), task('執行中', 0.75)]) === '約 7 秒後')
check('every repair done → 回溫中', done([task('完成', 1)]) === '維修已完成，回溫中（約 4 秒）', done([task('完成', 1)]))
check('Event.repaired_at (tasks trimmed) → 回溫中', done([], true) === '維修已完成，回溫中（約 4 秒）')
check('every repair done, no 回溫 on the server → 確認恢復中', fastDoneText([task('完成', 1)], OLD) === '維修已完成，確認恢復中')

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
    ours.push(Number(/約 (\d+) 秒/.exec(fastDoneText([t], F))[1]))
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

// ---------------------------------------------------------------- the phone card's words (cardText.ts, veteran 10-22)
// shaped like the server's press7_temp_drop card at REPAIR_FAST_S=12 (the 檢修 task is made before the decision, held 待派)
{
  const ct = await vite.ssrLoadModule('/src/phone/cardText.ts')
  const { optionChoices } = await vite.ssrLoadModule('/src/optionGuide.ts')
  const x = { machine_id: 'CURE-07', channel: 'temp_c', op: '>=', value: 121, value_hi: null, within_min: 30 }
  const ev = { id: 'EV-7', source: 'detector', machine_id: 'CURE-07', state: 'await_approval', hypotheses: [], constraints: [],
    anomaly: { channel: 'temp_c', value: 122.35, baseline: 128, message: '7 號 128 → 122°C，預測穩定在 122±1°C' },
    plan: { hypothesis: { id: 'H1', statement: '蒸汽壓不足，溫度會穩定', status: 'active', expectation: x }, steps: [] },
    steps: [{ step: { tool: 'cure_formula', args: { ref_cure_h: 24 } }, result: { ok: true, data: { total_cure_h: 28.5 } } }] }
  const card = { id: 'AP-7', event_id: 'EV-7', round: 1, title: '7 號機溫度下降 128→122.3°C', reason: '假設 H1：蒸汽壓不足，溫度會穩定',
    gaps: ['10/03 夜班缺 1 人'], delivery_impact: '不受影響', expires_at: '2026-10-03T03:54:25+08:00', changes: [],
    actions: ['S4 排程 v2 生效（7 號硫化 28.5 h；#A-311 改 6 號）', 'S4b 7 號機硫化總時數 28.5 h（公式 28.5 h）', '人力：依所選方案（A 夜班加班／B 從 2 線調 1 人）'],
    options: [{ id: 'A', label: '夜班加班', args: { kind: 'overtime' }, recommended: false }, { id: 'B', label: '從 2 線調 1 人', args: { kind: 'staff_move' }, recommended: true }] }
  const tasks = [{ id: 'TK-1', event_id: 'EV-7', kind: '檢修', title: '硫化機：7 號機溫度下降，請檢查蒸汽供應與溫度計', status: '待派', progress: 0, worker_name: null }]
  check('card title: 7 號硫化機溫度降到 122°C', ct.cardTitle(ev, card) === '7 號硫化機溫度降到 122°C', ct.cardTitle(ev, card))
  const say = ct.cardSentence(ev, card, tasks, F)
  check('card sentence: cause, who goes, the health seconds, when green', say === '疑似蒸汽壓不足。核准後 Agent Nemo 派人進廠維修，約 12 秒；溫度回到正常才轉綠。', say)
  check('card sentence: 3 s on a local server (never a written 12)', /約 3 秒/.test(ct.cardSentence(ev, card, tasks, OLD)))
  const does = ct.doLines(ev, card, tasks, F)
  check('核准後會做: the repair task\'s own words, the cure, the delivery (3 lines)',
    JSON.stringify(does) === JSON.stringify(['派人檢查蒸汽供應與溫度計（約 12 秒）', '這一模硫化延長到 28.5 小時（公式計算）', '訂單交期不受影響']), does.join('／'))
  check('no gap note when a 方案 covers the gap', ct.gapNote(card, true) === null)
  const ten = { ...card, gaps: [], actions: ['S4 排程 v3 生效（10 號硫化 41.1 h；無機台異動）', '人力：本次不含人力調動（10/03 夜班 從 2 線調 1 人 未列入核准，該班缺口維持原狀）'] }
  check('a card without staffing says the gap stays', ct.gapNote(ten, false) === '這次核准不含人力：10/03 夜班仍缺 1 人', ct.gapNote(ten, false))
  check('deadline: 03:54 AM 前 (date only on another day)', ct.deadlineText(card.expires_at, '2026-10-03T02:14:00+08:00') === '03:54 AM 前'
    && ct.deadlineText(card.expires_at, '2026-10-02T23:00:00+08:00') === '2026/10/03 03:54 AM 前')
  const ch = optionChoices(card, ev)
  check('方案 A says what it changes', ch[0]?.effect === '原班人員加班，不從別組調人' && !ch[0]?.why, JSON.stringify(ch[0]))
  check('方案 B says what it changes and why the system suggests it', ch[1]?.effect === '2 線少 1 人，可能影響產能・風險較高（跨組調人）' && ch[1]?.why === '不用加班，交期與 A 相同', JSON.stringify(ch[1]))
  // 「不要加班」 left only the transfer (Codex 10-22): no choice to make, but 核准 still moves someone, so the card says
  // that, never 「這次核准不含人力」
  const one = { ...card, actions: card.actions.slice(0, 2), options: [{ ...card.options[1], id: 'A' }] }
  const only = optionChoices(one, { ...ev, constraints: [{ kind: 'no_overtime' }] })
  check('one staffing option: still a staffing option, why = 照你說的', only.length === 1 && only[0].why === '照你說的', JSON.stringify(only))
  check('one staffing option: one 人力 line', ct.staffNote(only[0]) === '人力照你說的：從 2 線調 1 人（2 線少 1 人，可能影響產能）', ct.staffNote(only[0]))
  check('one staffing option: no 「不含人力」 gap note', ct.gapNote(one, true) === null)
  const bare = optionChoices(one, ev)
  check('one staffing option, nobody asked: no 「交期與  相同」 comparison', bare.length === 1 && bare[0].why === null, JSON.stringify(bare[0]))
  const rebased = { ...card, version: 2, changes: ['10 號機排程先生效；3 號機依現行排程重算：SO-004：4 號 → 2 號（10/03 14:00 入模）；SO-001：7 號 → 5 號；SO-004 共 2 模改機台', '人力沿用主管上次的選擇：B 從 2 線調 1 人'] }
  check('recomputed card: why, how many orders move, the same 方案', ct.rebasedText({ ...ev, machine_id: 'CURE-03' }, rebased)
    === '10 號硫化機的排程先生效了，3 號硫化機這張已依新排程重算：2 張訂單換機台，人力照你上次選的方案 B。請再核准一次。', ct.rebasedText({ ...ev, machine_id: 'CURE-03' }, rebased))
  const working = [{ ...tasks[0], status: '執行中', progress: 0.5, worker_name: '黃建宏' }]
  const l = ct.liveLine({ ...ev, state: 'execute' }, working, F)
  check('watching: one line, 維修中 and the seconds left', `${l.name}・${l.phase}・${l.secs}` === '7 號硫化機・維修中・約 6 秒' && l.who === '黃建宏正在檢查蒸汽供應與溫度計。溫度回到正常才轉綠。', `${l.name}・${l.phase}・${l.secs}｜${l.who}`)
  const feed = ct.liveLine({ ...ev, machine_id: 'B-OPEN', state: 'execute' }, [{ ...working[0], kind: '放慢投料' }], F)
  check('watching: a mill slowing its feed is 放慢投料中, not 維修中', feed.phase === '放慢投料中', feed.phase)
  check('watching: still planning → Agent Nemo 擬方案中', ct.liveLine({ ...ev, state: 'plan' }, [], F).phase === 'Agent Nemo 擬方案中')
  const stop = ct.liveLine({ ...ev, state: 'safe_stop' }, [], F)
  check('watching: a safe stop says a person has to act, not 正常', stop.phase === '已安全停止，需要人工處置' && /請到現場確認設備/.test(stop.who ?? ''), `${stop.phase}｜${stop.who}`)
}

await vite.close()
console.log(failed ? `${failed} FAILED` : 'ALL PASS')
process.exit(failed ? 1 : 0)
