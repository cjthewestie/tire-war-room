// Issue #46: the floor worker's task page in 中文 / Tiếng Việt / Bahasa Indonesia. Fixed phrases only, built
// from the task's structured fields (kind, machine, team, zone, orders) and the numbers code put in its title;
// no model is asked, so it works offline and says exactly what code decided. A kind without a phrase falls
// back to the Chinese title. Vietnamese and Indonesian are written for plant-floor readers; have a native
// speaker check them before using them with a customer.
import type { TaskStatus, TeamId, WorkTask, WorldZone } from '../contracts'
import { MACHINE_META } from '../factory'

export type Lang = 'zh' | 'vi' | 'id'
export const LANGS: { id: Lang; label: string }[] = [
  { id: 'zh', label: '中文' }, { id: 'vi', label: 'Tiếng Việt' }, { id: 'id', label: 'Bahasa Indonesia' },
]

type Tri = Record<Lang, string>

export const UI = {
  title: { zh: '現場任務', vi: 'Công việc tại xưởng', id: 'Tugas di lantai produksi' },
  who: { zh: '我是', vi: 'Tôi là', id: 'Saya' },
  all: { zh: '全部任務', vi: 'Tất cả công việc', id: 'Semua tugas' },
  none: { zh: '目前沒有任務', vi: 'Hiện chưa có việc', id: 'Belum ada tugas' },
  noneHint: {
    zh: '主管核准計劃後，派給你的工作會出現在這裡。',
    vi: 'Khi quản lý duyệt kế hoạch, việc giao cho bạn sẽ hiện ở đây.',
    id: 'Setelah supervisor menyetujui rencana, tugas untuk Anda akan muncul di sini.',
  },
  where: { zh: '地點', vi: 'Vị trí', id: 'Lokasi' },
  order: { zh: '訂單', vi: 'Đơn hàng', id: 'Pesanan' },
  time: { zh: '時間', vi: 'Thời gian', id: 'Waktu' },
  worker: { zh: '負責', vi: 'Người làm', id: 'Petugas' },
  unassigned: { zh: '還沒派人', vi: 'Chưa giao người', id: 'Belum ada petugas' },
  original: { zh: '中文原文', vi: 'Bản gốc tiếng Trung', id: 'Teks asli (Mandarin)' },
  done: { zh: '已完成', vi: 'Đã xong', id: 'Selesai' },
  live: { zh: '即時連線', vi: 'Đã kết nối', id: 'Terhubung' },
  offline: { zh: '連線中…', vi: 'Đang kết nối lại…', id: 'Menghubungkan ulang…' },
  // 人員回報 (CJ 2026-10-22: 「加回報」)
  start: { zh: '到達・開始', vi: 'Đã đến・Bắt đầu', id: 'Sudah tiba・Mulai' },
  finish: { zh: '完成', vi: 'Xong việc', id: 'Selesai' },
  note: { zh: '備註（可不填）', vi: 'Ghi chú (không bắt buộc)', id: 'Catatan (opsional)' },
  photo: { zh: '拍照（可不拍）', vi: 'Chụp ảnh (không bắt buộc)', id: 'Foto (opsional)' },
  photoKept: { zh: '已附照片', vi: 'Đã đính kèm ảnh', id: 'Foto terlampir' },
  photoRemove: { zh: '移除', vi: 'Bỏ ảnh', id: 'Hapus' },
  sending: { zh: '送出中…', vi: 'Đang gửi…', id: 'Mengirim…' },
  verifying: { zh: '等待程式確認讀值…', vi: 'Đang chờ hệ thống kiểm tra số đo…', id: 'Menunggu sistem memeriksa pembacaan…' },
  verified: { zh: '程式已確認讀值', vi: 'Hệ thống đã xác nhận số đo', id: 'Sistem sudah memastikan pembacaan' },
  reportedDone: { zh: '你已回報完成', vi: 'Bạn đã báo xong', id: 'Anda sudah lapor selesai' },
  overridden: { zh: '主管代為完成', vi: 'Quản lý đã xác nhận xong thay bạn', id: 'Supervisor menyelesaikan atas nama Anda' },
  pickMe: { zh: '先在上方選「我是」，才能回報', vi: 'Chọn "Tôi là" ở trên để báo cáo', id: 'Pilih "Saya" di atas untuk melapor' },
  pin: { zh: '請輸入你的人員 PIN', vi: 'Nhập mã PIN của bạn', id: 'Masukkan PIN Anda' },
  failed: { zh: '沒送出', vi: 'Chưa gửi được', id: 'Gagal dikirim' },
  overdue: { zh: '超過預計時間，做完請回報', vi: 'Đã quá giờ dự kiến, làm xong hãy báo', id: 'Lewat waktu perkiraan, lapor jika sudah selesai' },
} satisfies Record<string, Tri>

export const STATUS: Record<TaskStatus, Tri> = {
  待派: { zh: '待派', vi: 'Chờ giao', id: 'Menunggu' },
  前往中: { zh: '前往中', vi: 'Đang đến', id: 'Sedang menuju' },
  執行中: { zh: '執行中', vi: 'Đang làm', id: 'Sedang dikerjakan' },
  完成: { zh: '完成', vi: 'Xong', id: 'Selesai' },
}

export const KIND: Record<string, Tri> = {
  設定硫化時間: { zh: '設定硫化時間', vi: 'Cài thời gian lưu hóa', id: 'Atur waktu vulkanisir' },
  換模準備: { zh: '換模準備', vi: 'Chuẩn bị khuôn', id: 'Siapkan cetakan' },
  停用檢修: { zh: '停用檢修', vi: 'Dừng máy kiểm tra', id: 'Hentikan & periksa' },
  品管: { zh: '品管', vi: 'Kiểm tra chất lượng', id: 'Kontrol kualitas' },
  檢修: { zh: '檢修', vi: 'Kiểm tra sửa chữa', id: 'Periksa & perbaiki' },
  加班補位: { zh: '加班補位', vi: 'Làm thêm giờ', id: 'Lembur' },
  調派支援: { zh: '調派支援', vi: 'Điều sang hỗ trợ', id: 'Bantu tim lain' },
  宣達: { zh: '宣達', vi: 'Thông báo', id: 'Briefing' },
  暖機: { zh: '暖機', vi: 'Làm nóng máy', id: 'Pemanasan mesin' },
  補料: { zh: '補料', vi: 'Cấp liệu', id: 'Isi bahan' },
  盤點: { zh: '盤點', vi: 'Kiểm kê', id: 'Hitung stok' },
}

const TEAM: Record<TeamId, Tri> = {
  curing: { zh: '硫化機組', vi: 'tổ lưu hóa', id: 'tim vulkanisir' },
  body: { zh: '胎體／胎面膠組', vi: 'tổ thân lốp/mặt lốp', id: 'tim badan/tapak ban' },
  side: { zh: '胎側膠組', vi: 'tổ hông lốp', id: 'tim dinding samping ban' },
  bead_nylon: { zh: '鋼絲／尼龍組', vi: 'tổ tanh thép/nylon', id: 'tim kawat/nilon' },
  bigopen: { zh: '大開煉機組', vi: 'tổ máy cán hở lớn', id: 'tim mesin giling besar' },
  bond: { zh: '貼合組', vi: 'tổ ép dán', id: 'tim perekatan' },
  trim: { zh: '修邊組', vi: 'tổ cắt bavia', id: 'tim perapian' },
  maintenance: { zh: '維修組', vi: 'tổ bảo trì', id: 'tim perawatan' },
  qc: { zh: '品管', vi: 'tổ QC', id: 'tim QC' },
  supervisor: { zh: '主管', vi: 'quản lý', id: 'supervisor' },
}

const ZONE: Record<WorldZone, Tri> = {
  war_room: { zh: '戰情室', vi: 'phòng điều hành', id: 'ruang kendali' },
  staff: { zh: '員工待命區', vi: 'khu chờ nhân viên', id: 'area tunggu karyawan' },
  line_A: { zh: 'A 胎體／胎面膠', vi: 'dây chuyền A (thân/mặt lốp)', id: 'lini A (badan/tapak ban)' },
  line_B: { zh: 'B 胎側膠', vi: 'dây chuyền B (hông lốp)', id: 'lini B (dinding samping)' },
  line_C: { zh: 'C 鋼絲圈', vi: 'dây chuyền C (vòng tanh)', id: 'lini C (kawat bead)' },
  line_D: { zh: 'D 尼龍布', vi: 'dây chuyền D (vải nylon)', id: 'lini D (kain nilon)' },
  line_BOND: { zh: '貼合', vi: 'khu ép dán', id: 'area perekatan' },
  line_CURE: { zh: '硫化區', vi: 'khu lưu hóa', id: 'area vulkanisir' },
  break: { zh: '休息區', vi: 'khu nghỉ', id: 'ruang istirahat' },
  toilet: { zh: '廁所', vi: 'nhà vệ sinh', id: 'toilet' },
  dock: { zh: '碼頭', vi: 'khu bốc dỡ hàng', id: 'dok bongkar muat' },
  off_site: { zh: '廠外', vi: 'ngoài xưởng', id: 'di luar pabrik' },
}

// machine names by type (factory.ts MACHINE_META.name); CURE-nn and BOND-01 are spelled out below
const MACHINE_NAME: Record<string, Tri> = {
  密煉機: { zh: '密煉機', vi: 'máy trộn kín', id: 'mesin mixer' },
  開煉機: { zh: '開煉機', vi: 'máy cán hở', id: 'mesin giling (open mill)' },
  大開煉機: { zh: '大開煉機', vi: 'máy cán hở lớn', id: 'mesin giling besar' },
  小開煉機: { zh: '小開煉機', vi: 'máy cán hở nhỏ', id: 'mesin giling kecil' },
  壓延機: { zh: '壓延機', vi: 'máy cán láng', id: 'mesin kalender' },
  冷卻機: { zh: '冷卻機', vi: 'máy làm nguội', id: 'mesin pendingin' },
  捲取設備: { zh: '捲取設備', vi: 'thiết bị cuộn', id: 'alat penggulung' },
  油壓機: { zh: '油壓機', vi: 'máy ép thủy lực', id: 'mesin press hidrolik' },
  放線架: { zh: '放線架', vi: 'giá xả dây', id: 'rak pelepas kawat' },
  押出機: { zh: '押出機', vi: 'máy đùn', id: 'mesin ekstruder' },
  纏繞機: { zh: '纏繞機', vi: 'máy quấn', id: 'mesin penggulung kawat' },
  氣囊: { zh: '氣囊', vi: 'túi khí bọc vải', id: 'kantong udara pembungkus' },
  裁切機: { zh: '裁切機', vi: 'máy cắt', id: 'mesin potong' },
}

export function machineName(id: string | null, lang: Lang): string {
  if (!id) return ''
  const m = MACHINE_META[id]
  if (!m) return id
  if (m.line === 'CURE') return { zh: `${m.short} 號硫化機`, vi: `máy lưu hóa số ${m.short}`, id: `mesin vulkanisir no. ${m.short}` }[lang]
  if (m.line === 'BOND') return { zh: '貼合油壓機', vi: 'máy ép dán', id: 'mesin press perekat' }[lang]
  const n = MACHINE_NAME[m.name]?.[lang] ?? m.name
  return lang === 'zh' ? `${m.line} 線${m.name}` : lang === 'vi' ? `${n} dây chuyền ${m.line}` : `${n} lini ${m.line}`
}

export const teamName = (t: TeamId, lang: Lang) => TEAM[t]?.[lang] ?? t
export const zoneName = (z: string, lang: Lang) => ZONE[z as WorldZone]?.[lang] ?? z
export const kindName = (k: string, lang: Lang) => KIND[k]?.[lang] ?? k

/** Teams named in a 宣達 / 調派支援 title ("向硫化機組宣達…", "從胎側膠組調 1 人到硫化機組"), in title order. */
function teamsIn(title: string): TeamId[] {
  const hits = (Object.keys(TEAM) as TeamId[]).map((t) => [t, title.indexOf(TEAM[t].zh)] as const)
  return hits.filter(([, i]) => i >= 0).sort((a, b) => a[1] - b[1]).map(([t]) => t)
}

/** The task as one sentence in `lang`, or null when there is no phrase for its kind (caller shows the title). */
export function taskText(t: WorkTask, lang: Lang): string | null {
  if (lang === 'zh') return t.title
  const m = machineName(t.machine_id, lang)
  const o = t.order_ids.join(', ')
  const hours = t.title.match(/(\d+(?:\.\d+)?)\s*h\b/)?.[1]
  const at = t.title.match(/(\d{1,2}:\d{2})/)?.[1]
  const vi = lang === 'vi'
  switch (t.kind) {
    case '設定硫化時間':
      return vi ? `Nhập tổng thời gian lưu hóa ${hours ?? '?'} giờ trên bảng điều khiển ${m}.`
        : `Masukkan total waktu vulkanisir ${hours ?? '?'} jam di panel ${m}.`
    case '換模準備':
      return vi ? `Chuẩn bị khuôn${o ? ` cho đơn ${o}` : ''}, đưa đến ${m}${at ? `, vào khuôn lúc ${at}` : ''}.`
        : `Siapkan cetakan${o ? ` untuk pesanan ${o}` : ''}, bawa ke ${m}${at ? `, masuk cetakan pukul ${at}` : ''}.`
    case '停用檢修':
      return vi ? `Dừng ${m}: kiểm tra đường ống hơi và nhiệt kế.` : `Hentikan ${m}: periksa pipa uap dan termometer.`
    case '品管':
      return vi ? `Đánh dấu lốp đang trong khuôn ở ${m} là "chờ đánh giá", giao cho QC.`
        : `Tandai ban di dalam cetakan ${m} sebagai "menunggu keputusan", serahkan ke QC.`
    case '檢修':
      return vi ? `Kiểm tra và sửa chữa ${m}.` : `Periksa dan perbaiki ${m}.`
    case '加班補位':
      return vi ? 'Làm thêm giờ: kéo dài ca, thay vào vị trí ra/vào khuôn đang thiếu người.'
        : 'Lembur: perpanjang shift, gantikan posisi keluar/masuk cetakan yang kosong.'
    case '調派支援': {
      const [from, to] = teamsIn(t.title)
      return vi ? `Được điều${from ? ` từ ${teamName(from, lang)}` : ''} sang hỗ trợ ${teamName(to ?? t.team, lang)}: báo cáo với tổ trưởng.`
        : `Dipindah${from ? ` dari ${teamName(from, lang)}` : ''} untuk membantu ${teamName(to ?? t.team, lang)}: lapor ke ketua tim.`
    }
    case '宣達': {
      const team = teamName(teamsIn(t.title)[0] ?? t.team, lang)
      // page 11: 「向硫化機組宣達：7 號機延長硫化至 36.5 h（+8.5 h），已依新排程」 -> say the numbers too
      const ext = t.title.match(/(\d+)\s*號機延長硫化至\s*(\d+(?:\.\d+)?)\s*h(?:（([+-]\d+(?:\.\d+)?)\s*h）)?/)
      if (ext) {
        const [, no, total, plus] = ext
        return vi ? `Họp nhanh với ${team}: máy lưu hóa số ${no} kéo dài lưu hóa đến ${total} giờ${plus ? ` (${plus} giờ)` : ''}, làm theo lịch mới.`
          : `Briefing ke ${team}: mesin vulkanisir no. ${no} diperpanjang sampai ${total} jam${plus ? ` (${plus} jam)` : ''}, ikuti jadwal baru.`
      }
      return vi ? `Họp nhanh với ${team}: thông báo kế hoạch / lịch sản xuất mới.`
        : `Briefing ke ${team}: sampaikan rencana / jadwal produksi baru.`
    }
    case '暖機':
      return vi ? `Làm nóng ${m} 30 phút khi bắt đầu ca (SOP-02).` : `Panaskan ${m} selama 30 menit di awal shift (SOP-02).`
    case '補料':
      return vi ? `Mang cao su hỗn hợp đến ${m}${o ? ` cho đơn ${o}` : ''}.` : `Bawa kompon karet ke ${m}${o ? ` untuk pesanan ${o}` : ''}.`
    case '盤點':
      return vi ? 'Ra khu bốc dỡ hàng kiểm kê nguyên liệu đang thiếu, xác nhận giờ hàng về với nhà cung cấp.'
        : 'Ke dok bongkar muat, hitung stok bahan yang menipis, pastikan jadwal kedatangan dengan pemasok.'
    default:
      return null
  }
}

/** Where to go: the machine when there is one, else the zone. */
export function taskWhere(t: WorkTask, lang: Lang): string {
  return t.machine_id && MACHINE_META[t.machine_id] ? machineName(t.machine_id, lang) : zoneName(t.zone, lang)
}
