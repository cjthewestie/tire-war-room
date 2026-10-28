// 背景輕音樂（CJ 10-22：「幫我加個無版權輕音樂」）：完全在瀏覽器裡用 WebAudio 合成，沒有任何音檔、沒有版權問題、離線可用。
// 一段很慢的環境音 pad：四個和弦（Cmaj9 → Am9 → Fmaj9 → G6/9）各 8 秒，柔和的低通、慢慢淡入淡出，偶爾一顆很輕的撥弦音。
// 音量約 12%。只在 3D Demo 時播放；白板 Pitch 打開時淡出。M 鍵／🔊 按鈕切換靜音，靜音狀態記在 localStorage。
// 瀏覽器要使用者動作後才准出聲：自動開始的 Demo 會在第一次點擊或按鍵時才真的響（G／▶ Demo 開始本身就算）。
// 成本：每個和弦 4 個振盪器 + 1 個濾波器，排程用 setTimeout（每 8 秒一次），不碰 requestAnimationFrame，不影響 FPS。

const KEY = 'tp.music.muted'
const VOL = 0.12
const BAR_S = 8
// MIDI notes
const CHORDS: number[][] = [
  [48, 55, 62, 64, 71],   // Cmaj9: C3 G3 D4 E4 B4
  [45, 52, 59, 60, 67],   // Am9:   A2 E3 B3 C4 G4
  [41, 48, 55, 57, 64],   // Fmaj9: F2 C3 G3 A3 E4
  [43, 50, 57, 59, 64],   // G6/9:  G2 D3 A3 B3 E4
]
const PLUCKS = [72, 74, 76, 79, 81]
const hz = (m: number) => 440 * Math.pow(2, (m - 69) / 12)

let ctx: AudioContext | null = null
let master: GainNode | null = null
let filter: BiquadFilterNode | null = null
let timer: ReturnType<typeof setTimeout> | undefined
let bar = 0
let playing = false
let unlockArmed = false
const subs = new Set<() => void>()

function readMuted(): boolean { try { return window.localStorage.getItem(KEY) === '1' } catch { return false } }
let muted = typeof window === 'undefined' ? false : readMuted()

function emit() { for (const f of subs) f() }
export function subscribeMusic(f: () => void): () => void { subs.add(f); return () => { subs.delete(f) } }
export const musicMuted = (): boolean => muted
export const musicPlaying = (): boolean => playing

function ensure(): AudioContext | null {
  if (ctx) return ctx
  try {
    const AC = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
    if (!AC) return null
    ctx = new AC()
    filter = ctx.createBiquadFilter(); filter.type = 'lowpass'; filter.frequency.value = 1100; filter.Q.value = 0.4
    master = ctx.createGain(); master.gain.value = 0
    filter.connect(master); master.connect(ctx.destination)
  } catch { ctx = null }
  return ctx
}

/** The first click / key after a demo that started by itself: let the music start. */
function armUnlock() {
  if (unlockArmed || typeof window === 'undefined') return
  unlockArmed = true
  const go = () => {
    window.removeEventListener('pointerdown', go, true); window.removeEventListener('keydown', go, true)
    unlockArmed = false
    if (ctx && ctx.state === 'suspended') void ctx.resume().then(() => { if (playing) fadeTo(muted ? 0 : VOL, 2) }).catch(() => {})
  }
  window.addEventListener('pointerdown', go, true); window.addEventListener('keydown', go, true)
}

function fadeTo(v: number, s: number) {
  if (!ctx || !master) return
  const t = ctx.currentTime
  master.gain.cancelScheduledValues(t)
  master.gain.setValueAtTime(master.gain.value, t)
  master.gain.linearRampToValueAtTime(v, t + s)
}

function voice(freq: number, t0: number, dur: number, peak: number, type: OscillatorType, out: AudioNode) {
  if (!ctx) return
  const o = ctx.createOscillator(), g = ctx.createGain()
  o.type = type; o.frequency.value = freq
  o.detune.value = (Math.random() - 0.5) * 8
  g.gain.setValueAtTime(0.0001, t0)
  g.gain.linearRampToValueAtTime(peak, t0 + dur * 0.35)
  g.gain.linearRampToValueAtTime(peak * 0.8, t0 + dur * 0.7)
  g.gain.linearRampToValueAtTime(0.0001, t0 + dur + 1.5)
  o.connect(g); g.connect(out)
  o.start(t0); o.stop(t0 + dur + 1.6)
}

function pluck(freq: number, t0: number, out: AudioNode) {
  if (!ctx) return
  const o = ctx.createOscillator(), g = ctx.createGain()
  o.type = 'triangle'; o.frequency.value = freq
  g.gain.setValueAtTime(0.0001, t0)
  g.gain.exponentialRampToValueAtTime(0.05, t0 + 0.02)
  g.gain.exponentialRampToValueAtTime(0.0001, t0 + 2.2)
  o.connect(g); g.connect(out)
  o.start(t0); o.stop(t0 + 2.3)
}

function scheduleBar() {
  if (!playing || !ctx || !filter) return
  const t0 = ctx.currentTime + 0.05
  const chord = CHORDS[bar % CHORDS.length]
  chord.forEach((m, i) => voice(hz(m), t0, BAR_S, i === 0 ? 0.11 : 0.07, i === 0 ? 'sine' : 'triangle', filter!))
  // two soft plucks somewhere in the bar
  for (let k = 0; k < 2; k++) if (Math.random() < 0.75) pluck(hz(PLUCKS[Math.floor(Math.random() * PLUCKS.length)]), t0 + 1.5 + Math.random() * 5, filter)
  // a slow filter breath
  filter.frequency.setValueAtTime(900, t0); filter.frequency.linearRampToValueAtTime(1400, t0 + BAR_S / 2); filter.frequency.linearRampToValueAtTime(900, t0 + BAR_S)
  bar++
  timer = setTimeout(scheduleBar, BAR_S * 1000)
}

/** Start the music (the 3D demo began). Silent while muted; plays once the browser allows audio. */
export function startMusic() {
  const c = ensure()
  if (!c) return
  if (!playing) { playing = true; bar = 0; scheduleBar(); emit() }
  if (c.state === 'suspended') { void c.resume().catch(() => {}); armUnlock() }
  fadeTo(muted ? 0 : VOL, 3)
}

/** Fade out and stop (the demo ended, or the pitch slides opened). */
export function stopMusic(fadeS = 2.5) {
  if (!playing) return
  playing = false
  clearTimeout(timer)
  fadeTo(0, fadeS)
  emit()
}

export function setMusicMuted(m: boolean) {
  muted = m
  try { window.localStorage.setItem(KEY, m ? '1' : '0') } catch { /* storage blocked */ }
  if (playing) fadeTo(m ? 0 : VOL, 0.6)
  emit()
}
export const toggleMusic = () => setMusicMuted(!muted)
