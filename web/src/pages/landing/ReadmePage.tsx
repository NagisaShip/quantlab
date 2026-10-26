import { useEffect, useState } from 'react'
import { Markdown } from '../../components/markdown'
import readmeZh from '../../../../README.md?raw'
import readmeEn from '../../../../README.en.md?raw'
import '../../stylesLandingReadme.css'
import { lang as uiLang } from '../../i18n'

/** 从 README 中移除会依赖仓库静态目录的徽章与截图，只保留正文。 */
function cleanReadme(text: string) {
  return text
    .replace(/<div align="center">[\s\S]*?<\/div>/gi, '')
    .replace(/\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)/g, '')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/<img[^>]*>/gi, '')
}

export function ReadmePage({ onClose }: { onClose: () => void }) {
  const [lang, setLang] = useState<'zh' | 'en'>(uiLang)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  const text = lang === 'zh' ? readmeZh : readmeEn
  const copy = lang === 'zh'
    ? { back: '← 返回介绍页', sub: '项目说明', aria: '项目 README', langAria: 'README 语言' }
    : { back: '← Back to overview', sub: 'Project overview', aria: 'Project README', langAria: 'README language' }
  return (
    <section className="lp-readme" role="dialog" aria-modal="true" aria-label={copy.aria} data-no-i18n>
      <header className="lp-readme-bar">
        <button type="button" className="lp-readme-back" onClick={onClose}>{copy.back}</button>
        <div className="lp-readme-title"><b>QuantLab v0.1.0-preview.2 README</b><span>{copy.sub}</span></div>
        <div className="lp-readme-lang" role="tablist" aria-label={copy.langAria}>
          <button type="button" role="tab" aria-selected={lang === 'zh'} className={lang === 'zh' ? 'on' : ''} onClick={() => setLang('zh')}>中文</button>
          <button type="button" role="tab" aria-selected={lang === 'en'} className={lang === 'en' ? 'on' : ''} onClick={() => setLang('en')}>English</button>
        </div>
      </header>
      <div className="lp-readme-scroll">
        <article className="lp-readme-doc">
          <Markdown text={cleanReadme(text)} />
        </article>
      </div>
    </section>
  )
}
