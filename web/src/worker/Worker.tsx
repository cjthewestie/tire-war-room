// Issue #46: /worker, the floor worker's phone. Pick yourself (or see every task), read what to do in 中文 /
// Tiếng Việt / Bahasa Indonesia. Read-only: the tasks come from the approved plan (warroom/sim/work.py) through
// the same world feed the dashboard uses; nothing here changes the plant.
import { useEffect, useMemo, useState } from 'react'
import type { TaskStatus, WorkTask, WorldWorker } from '../contracts'
import { fmtHM, fmtMD } from '../format'
import { useWarRoom } from '../useWarRoom'
import { LANGS, STATUS, UI, kindName, taskText, taskWhere, teamName, type Lang } from './i18n'

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

  const shown = useMemo(() => {
    const mine = eff === ALL ? tasks : tasks.filter((t) => t.worker_id === eff)
    const open = mine.filter((t) => t.status !== '完成')
      .sort((a, b) => ORDER[a.status] - ORDER[b.status] || a.created_at.localeCompare(b.created_at))
    const done = mine.filter((t) => t.status === '完成')
      .sort((a, b) => (b.done_at ?? '').localeCompare(a.done_at ?? '')).slice(0, DONE_SHOWN)
    return { open, done }
  }, [tasks, eff])

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
        {shown.open.map((t) => <TaskCard key={t.id} t={t} lang={lang} showWorker={eff === ALL} />)}
      </ul>
      {shown.done.length > 0 && (
        <>
          <h2 className="wk-h2">{UI.done[lang]}</h2>
          <ul className="wk-list done">
            {shown.done.map((t) => <TaskCard key={t.id} t={t} lang={lang} showWorker={eff === ALL} />)}
          </ul>
        </>
      )}
    </div>
  )
}

function personLabel(w: WorldWorker, lang: Lang): string {
  return `${w.name}・${teamName(w.team, lang)}`
}

function TaskCard({ t, lang, showWorker }: { t: WorkTask; lang: Lang; showWorker: boolean }) {
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
      {lang !== 'zh' && <p className="wk-orig"><span>{UI.original[lang]}</span>{t.title}</p>}
    </li>
  )
}

const statusKey = (s: TaskStatus) => ({ 待派: 'wait', 前往中: 'go', 執行中: 'work', 完成: 'done' } as const)[s]
