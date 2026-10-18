import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { ImageViewerHost } from './components/ImageViewer'
import App from './App'
import { loadLang, startDomTranslator } from './i18n'
import { startMobileTableCards } from './mobileTables'
import { applyPrefs, watchSystemTheme } from './components/prefs'
import '@fontsource-variable/inter'
import '@fontsource-variable/jetbrains-mono'
import './styles.css'
import './styles2.css'
import './styles3.css'
import './stylesLab.css'
import './stylesChat.css'
import './stylesClaude.css'
import './stylesAskAI.css'
import './stylesRuleCode.css'
import './stylesBlocks.css'
import './stylesFactor.css'
import './stylesTerminal.css'
import './stylesConv.css'
import './stylesBot.css'
import './stylesResearch.css'
import './stylesLayout.css'
import './stylesCollab.css'
import './stylesQmoji.css'
import './stylesMotion.css'
import './stylesPublic.css'
import './stylesResponsiveTables.css'

applyPrefs()
watchSystemTheme()

// 手机看板可以「添加到主屏幕」：本机开发时不注册（热更新会被缓存打乱）
if (!['localhost', '127.0.0.1'].includes(location.hostname) && location.port !== '5173' && 'serviceWorker' in navigator) {
  window.addEventListener('load', () => { navigator.serviceWorker.register('/sw.js').catch(() => undefined) })
}

// 英文界面：先加载词典并开始翻译，再渲染（避免先闪一下中文）
loadLang().catch(() => undefined).finally(() => {
  startDomTranslator()
  startMobileTableCards()
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App />
      <ImageViewerHost />
    </StrictMode>,
  )
})
