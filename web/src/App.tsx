// Placeholder war-room shell. The web builder replaces this with the page-2 dashboard.
import { useLiveStream } from './api'

export default function App() {
  const { snapshot, connected } = useLiveStream()
  return (
    <main className="shell">
      <header>
        <h1>實心胎智慧製造戰情室</h1>
        <span className={connected ? 'chip ok' : 'chip bad'}>{connected ? '已連線' : '未連線'}</span>
      </header>
      <p>
        {snapshot
          ? `正常 ${snapshot.header.normal_count} 台 / 異常 ${snapshot.header.abnormal_count} 台`
          : '等待資料…'}
      </p>
    </main>
  )
}
