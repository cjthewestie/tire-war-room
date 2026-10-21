// Issue #46: /worker, the floor worker's phone. Pick yourself (or see every task), read what to do in 中文 /
// Tiếng Việt / Bahasa Indonesia. The tasks come from the approved plan (warroom/sim/work.py) through the same world
// feed the dashboard uses. In 人員回報 mode (CJ 2026-10-22: 「加回報」) the assigned worker taps 到達・開始 and 完成
// (optional note and photo); code then checks the reading (VERIFY) before the event closes. Nothing here moves a
// machine.
import { useEffect, useMemo, useRef, useState } from 'react'
import type { Event, TaskStatus, WorkTask, WorldWorker } from '../contracts'
import { fmtHM, fmtMD } from '../format'
import { useWarRoom } from '../useWarRoom'
import { LANGS, STATUS, UI, kindName, taskText, taskWhere, teamName, type Lang } from './i18n'
import { ReportError, report, shrinkPhoto } from './report'

const ALL = '__all__'
const ORDER: Record<TaskStatus, number> = { 執行中: 0, 前往中: 1, 待派: 2, 完成: 3 }
const DONE_SHOWN = 5
const HTML_LANG: Record<Lang, string> = { zh: 'zh-Hant', vi: 'vi', id: 'id' }

function load(key: string, fallback: string): string {
  try { return localStorage.getItem(key) || fallback } catch { return fallback }
}
function keep(key: string, value: string) {
  try { localStorage.setItem(key, value) } catch { /* private mode: the choice lasts this visit */ }
}

export function Worker() {
  const { state, connected } = useWarRoom()
  const world = state.world
  const [lang, setLang] = useState<Lang>(() => (load('worker-lang', 'zh') as Lang))
  const [who, setWho] = useState<string>(() => new URLSearchParams(location.search).get('w') || load('worker-id', ALL))
  useEffect(() => { document.documentElement.lang = HTML_LANG[lang]; keep('worker-lang', lang) }, [lang])
  useEffect(() => { keep('worker-id', who) }, [who])

  const taskView = world?.tasks
  const peopleView = world?.people
  const tasks = useMemo(() => taskView?.items ?? [], [taskView])
  const workers = useMemo(() => peopleView?.workers ?? [], [peopleView])
  // people who can be picked: on duty now, or holding a task
  const pickable = useMemo(() => {
    const busy = new Set(tasks.map((t) => t.worker_id).filter(Boolean))
    return workers.filter((w) => busy.has(w.id) || !['下班', '請假'].includes(w.state) && w.zone !== 'off_site')
      .sort((a, b) => Number(busy.has(b.id)) - Number(busy.has(a.id)) || a.name.localeCompare(b.name, 'zh-Hant'))
  }, [tasks, workers])
  const me = workers.find((w) => w.id === who) ?? null
  // a remembered person who is not in this plant (another scenario) falls back to every task once the world is in
  const eff = who === ALL || me || !world ? who : ALL

  const events = state.snapshot?.events
  // 完成 reported, the event still open: code has not confirmed the reading yet (VERIFY)
  const verifying = useMemo(() => {
    const open = new Set((events ?? []).filter((e: Event) => e.state !== 'closed' && e.state !== 'safe_stop').map((e) => e.id))
    return (t: WorkTask) => t.mode === 'report' && t.status === '完成' && !!t.event_id && open.has(t.event_id)
  }, [events])
  const shown = useMemo(() => {
    const mine = eff === ALL ? tasks : tasks.filter((t) => t.worker_id === eff)
    const open = mine.filter((t) => t.status !== '完成' || verifying(t))
      .sort((a, b) => ORDER[a.status] - ORDER[b.status] || a.created_at.localeCompare(b.created_at))
    const done = mine.filter((t) => t.status === '完成' && !verifying(t))
      .sort((a, b) => (b.done_at ?? '').localeCompare(a.done_at ?? '')).slice(0, DONE_SHOWN)
    return { open, done }
  }, [tasks, eff, verifying])

  const ft = world?.factory_time
  return (
    <div className="wk">
      <header className="wk-top">
        <div className="wk-row">
          <h1>{UI.title[lang]}</h1>
          <span className={`wk-conn ${connected ? 'ok' : ''}`}>{connected ? UI.live[lang] : UI.offline[lang]}</span>
          {ft && <span className="wk-clock">{fmtMD(ft)} {fmtHM(ft)}</span>}
        </div>
        <div className="wk-langs" role="group" aria-label="Language / 語言">
          {LANGS.map((l) => (
            <button key={l.id} type="button" lang={HTML_LANG[l.id]} aria-pressed={lang === l.id} onClick={() => setLang(l.id)}>{l.label}</button>
          ))}
        </div>
        <label className="wk-who">
          <span>{UI.who[lang]}</span>
          <select value={eff} onChange={(e) => setWho(e.target.value)} data-testid="wk-who">
            <option value={ALL}>{UI.all[lang]}</option>
            {pickable.map((w) => <option key={w.id} value={w.id}>{personLabel(w, lang)}</option>)}
          </select>
        </label>
      </header>

      {shown.open.length === 0 && (
        <div className="wk-empty" data-testid="wk-empty">
          <b>{UI.none[lang]}</b>
          <p>{UI.noneHint[lang]}</p>
        </div>
      )}
      <ul className="wk-list" data-testid="wk-open">
        {shown.open.map((t) => <TaskCard key={t.id} t={t} lang={lang} showWorker={eff === ALL} me={eff === ALL ? null : eff} verifying={verifying(t)} />)}
      </ul>
      {shown.done.length > 0 && (
        <>
          <h2 className="wk-h2">{UI.done[lang]}</h2>
          <ul className="wk-list done">
            {shown.done.map((t) => <TaskCard key={t.id} t={t} lang={lang} showWorker={eff === ALL} me={eff === ALL ? null : eff} verifying={false} />)}
          </ul>
        </>
      )}
    </div>
  )
}

function personLabel(w: WorldWorker, lang: Lang): string {
  return `${w.name}・${teamName(w.team, lang)}`
}

function TaskCard({ t, lang, showWorker, me, verifying }: { t: WorkTask; lang: Lang; showWorker: boolean; me: string | null; verifying: boolean }) {
  const text = taskText(t, lang) ?? t.title
  const moving = t.status === '前往中' || t.status === '執行中'
  const when = t.status === '完成' ? t.done_at : t.eta ?? t.assigned_at ?? t.created_at
  return (
    <li className={`wk-card s-${statusKey(t.status)}`} data-testid="wk-task">
      <div className="wk-card-top">
        <span className="wk-kind">{kindName(t.kind, lang)}</span>
        <span className="wk-status">{STATUS[t.status][lang]}</span>
      </div>
      <p className="wk-text" lang={HTML_LANG[lang]}>{text}</p>
      {moving && <div className="wk-bar" aria-hidden="true"><i style={{ width: `${Math.round(Math.min(1, Math.max(0, t.progress)) * 100)}%` }} /></div>}
      <dl className="wk-meta">
        <div><dt>{UI.where[lang]}</dt><dd>{taskWhere(t, lang)}</dd></div>
        {t.order_ids.length > 0 && <div><dt>{UI.order[lang]}</dt><dd>{t.order_ids.join('、')}</dd></div>}
        {when && <div><dt>{UI.time[lang]}</dt><dd>{fmtHM(when)}</dd></div>}
        {showWorker && <div><dt>{UI.worker[lang]}</dt><dd>{t.worker_name ?? UI.unassigned[lang]}</dd></div>}
      </dl>
      {t.mode === 'report' && t.status === '執行中' && t.overdue && <p className="wk-overdue" role="status">{UI.overdue[lang]}</p>}
      {t.mode === 'report' && (t.status === '前往中' || t.status === '執行中') && (
        me !== null && me === t.worker_id ? <ReportControls t={t} lang={lang} me={me} />
          : me === null ? <p className="wk-hint">{UI.pickMe[lang]}</p> : null
      )}
      {t.mode === 'report' && t.status === '完成' && (
        <p className={`wk-verify ${verifying ? 'wait' : 'ok'}`} role="status" data-testid="wk-verify">
          <b>{t.override_reason ? UI.overridden[lang] : UI.reportedDone[lang]}{t.done_at ? ` ${fmtHM(t.done_at)}` : ''}{t.photo_sha256 ? `・${UI.photoKept[lang]}` : ''}</b>
          <span>{verifying ? UI.verifying[lang] : UI.verified[lang]}</span>
        </p>
      )}
      {lang !== 'zh' && <p className="wk-orig"><span>{UI.original[lang]}</span>{t.title}</p>}
    </li>
  )
}

const statusKey = (s: TaskStatus) => ({ 待派: 'wait', 前往中: 'go', 執行中: 'work', 完成: 'done' } as const)[s]

function ReportControls({ t, lang, me }: { t: WorkTask; lang: Lang; me: string }) {
  const [note, setNote] = useState('')
  const [photo, setPhoto] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const file = useRef<HTMLInputElement>(null)
  const done = t.status === '執行中'
  const send = async () => {
    setBusy(true); setErr(null)
    try {
      await report(t.id, done ? 'done' : 'start',
        { worker_id: me, ...(note.trim() ? { note: note.trim() } : {}), ...(done && photo ? { photo } : {}) },
        () => window.prompt(UI.pin[lang]))
      setNote(''); setPhoto(null)
    } catch (e) {
      setErr(e instanceof ReportError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }
  const pick = async (f: File | undefined) => {
    if (!f) return
    try { setPhoto(await shrinkPhoto(f)) } catch (e) { setErr(String(e)) }
    if (file.current) file.current.value = ''
  }
  return (
    <div className="wk-report" data-testid="wk-report">
      {done && (
        <>
          <input className="wk-note" type="text" maxLength={300} value={note} onChange={(e) => setNote(e.target.value)}
            placeholder={UI.note[lang]} aria-label={UI.note[lang]} lang={HTML_LANG[lang]} />
          {photo ? (
            <div className="wk-photo">
              <img src={photo} alt="" />
              <span>{UI.photoKept[lang]}</span>
              <button type="button" className="wk-link" onClick={() => setPhoto(null)}>{UI.photoRemove[lang]}</button>
            </div>
          ) : (
            <label className="wk-photo-btn">
              <input ref={file} type="file" accept="image/jpeg,image/png,image/webp" capture="environment"
                onChange={(e) => pick(e.target.files?.[0])} data-testid="wk-photo" />
              {UI.photo[lang]}
            </label>
          )}
        </>
      )}
      <button type="button" className={`wk-go ${done ? 'done' : 'start'}`} disabled={busy} onClick={send}
        data-testid={done ? 'wk-done' : 'wk-start'}>
        {busy ? UI.sending[lang] : done ? UI.finish[lang] : UI.start[lang]}
      </button>
      {err && <p className="wk-err" role="alert">{UI.failed[lang]}：{err}</p>}
    </div>
  )
}
