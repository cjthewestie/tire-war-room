// 訂單變更申請: a front-end-only preview of a change request. Only 承諾交期 and 優先級 can change; 訂購數量 is read-only
// (a different quantity needs a new order). A 變更原因 (type + editable explanation) and the 內部核准確認 checkbox are required
// before 預覽變更內容 (type starts as 「請選擇變更原因」 with a blank explanation; at least one of 承諾交期 / 優先級 must really
// change). The preview lists only the fields that really changed and says 「尚未生效」. Nothing is sent anywhere:
// no order API, no agent, no re-schedule, the order in the world state is never touched, and nothing says saved / submitted
// / approved. State lives in this component, so closing and reopening starts clean (checkbox unchecked).
import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import type { WorldOrder } from '../contracts'
import { SIZE_LABEL } from '../factory'
import { dueKey } from './dueWeek'

const REASONS = [
  { k: '客戶需求變更', text: '客戶要求提前交貨，需重新評估產能及交期。' },
  { k: '生產／設備異常', text: '硫化機發生異常，影響原定生產排程，需重新評估交期。' },
  { k: '原物料供應問題', text: '原料供應延遲，影響投料時間及後續交貨計畫。' },
  { k: '其他', text: '' },
]
const presetOf = (k: string) => REASONS.find((r) => r.k === k)?.text ?? ''

export function OrderEditDialog({ o, onClose }: { o: WorldOrder; onClose: () => void }) {
  const due0 = dueKey(o.due)
  const [due, setDue] = useState(due0)
  const [rush, setRush] = useState(o.rush)
  const [type, setType] = useState('')
  const [reason, setReason] = useState('')
  const [approved, setApproved] = useState(false)
  const [preview, setPreview] = useState(false)
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [onClose])
  const edit = <T,>(set: (v: T) => void) => (v: T) => { set(v); setPreview(false) }
  // Switching the type swaps the demo text only while the box is blank or still holds the previous demo text;
  // text the user typed is kept and a button offers the new type's demo text instead.
  const untouched = reason.trim() === '' || reason === presetOf(type)
  const pickType = (k: string) => { setType(k); if (untouched) setReason(presetOf(k)); setPreview(false) }
  const custom = !untouched && reason !== presetOf(type)
  const dueErr = due ? null : '請選擇承諾交期'
  const rows = [
    { k: '承諾交期', before: due0 || '—', after: due || '—', changed: due !== due0 },
    { k: '優先級', before: o.rush ? '急單' : '一般', after: rush ? '急單' : '一般', changed: rush !== o.rush },
  ].filter((r) => r.changed)
  const missing = [dueErr, !dueErr && !rows.length && '請先調整承諾交期或優先級。', !type && '請選擇變更原因', type && !reason.trim() && '請填寫變更原因說明', !approved && '請勾選「已取得權責單位主管核准」'].filter(Boolean) as string[]
  return createPortal(
    <div className="oe-wrap" onClick={onClose}>
      <div className="oe" role="dialog" aria-modal="true" aria-label={`訂單變更申請 ${o.id}`} data-testid="order-edit" onClick={(e) => e.stopPropagation()}>
        <header className="oe-h">
          <h3>訂單變更申請 <span className="muted">{o.id}</span></h3>
          <button type="button" className="oe-x" onClick={onClose} aria-label="關閉">×</button>
        </header>
        <div className="oe-form">
          <label><span>訂單編號</span><input value={o.id} readOnly /></label>
          <label><span>客戶</span><input value={o.customer} readOnly /></label>
          <label><span>胎型</span><input value={SIZE_LABEL[o.size]} readOnly /></label>
          <label><span>訂購數量</span><input value={`${o.qty} 條`} readOnly data-testid="order-edit-qty" /></label>
          <p className="oe-qty-note wide" data-testid="order-edit-qty-note">訂購數量不得修改；如需變更數量，請另行新增訂單。</p>
          <label><span>承諾交期</span><input type="date" autoFocus value={due} onChange={(e) => edit(setDue)(e.target.value)} aria-invalid={!!dueErr} /></label>
          <label><span>優先級</span>
            <select value={rush ? 'rush' : 'normal'} onChange={(e) => edit(setRush)(e.target.value === 'rush')}><option value="normal">一般</option><option value="rush">急單</option></select></label>
          <fieldset className="oe-reason wide">
            <legend>變更原因</legend>
            <label><span>原因類型</span>
              <select value={type} data-testid="order-edit-type" onChange={(e) => pickType(e.target.value)}><option value="">請選擇變更原因</option>{REASONS.map((r) => <option key={r.k} value={r.k}>{r.k}</option>)}</select></label>
            <label><span>變更原因說明</span>
              <textarea rows={3} maxLength={200} value={reason} data-testid="order-edit-reason" placeholder="請輸入變更原因說明" onChange={(e) => edit(setReason)(e.target.value)} aria-invalid={!reason.trim()} /></label>
            {custom && type && type !== '其他' && <button type="button" className="oe-link" onClick={() => { setReason(presetOf(type)); setPreview(false) }}>改用此類型的示範文字</button>}
          </fieldset>
          <fieldset className="oe-approve wide">
            <legend>內部核准確認</legend>
            <label className="oe-check"><input type="checkbox" checked={approved} data-testid="order-edit-approve" onChange={(e) => edit(setApproved)(e.target.checked)} /><span>已取得權責單位主管核准</span></label>
            <p className="muted small">請確認已依公司內部程序取得必要核准。</p>
          </fieldset>
        </div>
        {preview && !missing.length && (
          <section className="oe-preview" data-testid="order-edit-preview" aria-label="變更內容預覽">
            <p><span className="oe-badge">尚未生效</span></p>
            <p className="muted small">以下僅供確認變更內容，尚未更新正式訂單及生產排程。</p>
            <table>
                <thead><tr><th>變更項目</th><th>修改前</th><th>修改後</th></tr></thead>
                <tbody>{rows.map((r) => <tr key={r.k} className="chg"><td>{r.k}</td><td>{r.before}</td><td>{r.after}</td></tr>)}</tbody>
            </table>
            <p className="oe-pv-reason"><b>變更原因：{type}</b><br />{reason.trim()}</p>
            <p className="oe-pv-ok">已勾選：已取得權責單位主管核准</p>
            <p className="note">修改可能影響既有生產排程，正式變更需重新評估。</p>
          </section>
        )}
        <footer className="oe-f">
          {!preview && missing.length > 0 && <p className="oe-missing" role="status">{missing[0]}</p>}
          <button type="button" className="btn-ghost" onClick={onClose}>{preview ? '關閉' : '取消'}</button>
          {preview && <button type="button" className="btn-ghost" onClick={() => setPreview(false)}>返回編輯</button>}
          {!preview && <button type="button" className="btn-approve" disabled={missing.length > 0} onClick={() => setPreview(true)}>預覽變更內容</button>}
        </footer>
      </div>
    </div>,
    document.body,
  )
}
