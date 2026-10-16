import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { applyPrefs, watchSystemTheme } from './components/prefs'
import './styles.css'
import './styles2.css'
import './styles3.css'
import './stylesLab.css'
import './stylesChat.css'
import './stylesClaude.css'
import './stylesAskAI.css'
import './stylesRuleCode.css'
import './stylesFactor.css'
import './stylesTerminal.css'
import './stylesConv.css'
import './stylesBot.css'
import './stylesResearch.css'
import './stylesMotion.css'

applyPrefs()
watchSystemTheme()

// 手机看板可以「添加到主屏幕」：本机开发时不注册（热更新会被缓存打乱）
if (!['localhost', '127.0.0.1'].includes(location.hostname) && location.port !== '5173' && 'serviceWorker' in navigator) {
  window.addEventListener('load', () => { navigator.serviceWorker.register('/sw.js').catch(() => undefined) })
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
