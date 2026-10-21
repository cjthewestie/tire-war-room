// 後台 shared error line: PIN / factory-network refusals in words.
export function Err({ err }: { err: string | null }) {
  if (!err) return null
  return <p className="note admin-err" role="alert">{/^40[13]/.test(err) ? `沒有權限：${err}。後台只限廠內網路，遠端需輸入主管 PIN。` : `讀取失敗：${err}`}</p>
}

