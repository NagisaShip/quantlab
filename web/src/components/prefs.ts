/** 界面偏好：主题（浅色 / 夜间 / 跟随系统）与涨跌配色（红涨绿跌 / 绿涨红跌）。存在本机浏览器。 */

export type ThemePref = 'light' | 'dark' | 'system'
export type UpDown = 'red' | 'green'

const read = (k: string) => {
  try {
    return localStorage.getItem(k)
  } catch {
    return null
  }
}
const write = (k: string, v: string) => {
  try {
    localStorage.setItem(k, v)
  } catch {
    /* 隐私模式等场景下不保存 */
  }
}

export const getTheme = (): ThemePref => (read('qa.theme') as ThemePref) || 'system'
export const getUpDown = (): UpDown => (read('qa.updown') as UpDown) || 'red'

export function resolvedTheme(pref: ThemePref = getTheme()): 'light' | 'dark' {
  if (pref !== 'system') return pref
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export function applyPrefs() {
  const root = document.documentElement
  root.dataset.theme = resolvedTheme()
  root.dataset.updown = getUpDown()
}

export function setTheme(pref: ThemePref) {
  write('qa.theme', pref)
  applyPrefs()
}

export function setUpDown(v: UpDown) {
  write('qa.updown', v)
  applyPrefs()
}

/** 跟随系统时，系统切换深浅色要即时生效。 */
export function watchSystemTheme() {
  const mq = window.matchMedia?.('(prefers-color-scheme: dark)')
  const on = () => { if (getTheme() === 'system') applyPrefs() }
  mq?.addEventListener?.('change', on)
}

/** 本机小偏好（侧栏开合、上次选的模型）：读写失败（隐私模式）时静默退回默认值。 */
export const store = {
  get: (k: string, d = '') => { try { return localStorage.getItem(k) ?? d } catch { return d } },
  set: (k: string, v: string) => { try { localStorage.setItem(k, v) } catch { /* 隐私模式 */ } },
}
