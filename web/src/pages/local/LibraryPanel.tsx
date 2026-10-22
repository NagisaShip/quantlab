import { useLocal } from './context'
import { Section } from './shared'

/** 设置 · 跨项目模块库：发布当前项目的固定版本，或把已发布版本导入当前项目。 */
export default function LibraryPanel() {
  const s = useLocal()
  const locked = s.busy !== ''

  return (
    <Section
      title="跨项目模块库"
      note="固定函数版本与源码指纹；旧项目不会因库升级而自动改变。发布后版本冻结，不能覆盖。"
    >
      <div className="lp-grid">
        <div className="lp-block">
          <div className="lp-block-head"><b>发布当前项目的模块</b></div>
          <div className="lp-field">
            <span>选择模块或函数</span>
            <select
              aria-label="发布到本机模块库"
              value={s.module}
              disabled={locked || s.cards.length === 0}
              onChange={(e) => s.setModule(e.target.value)}
            >
              <option value="">{s.cards.length ? '选择当前项目的模块或函数' : '当前项目还没有可发布的模块'}</option>
              {s.cards.map((card) => (
                <option key={`${card.kind}@${card.version}`} value={`${card.kind}@${card.version}`}>
                  {card.title} · {card.kind}@{card.version}
                </option>
              ))}
            </select>
            <small>模块来自当前项目的工作流卡片；发布时会记录定义源码与指纹。</small>
          </div>
          <div className="lp-inline">
            <button type="button" className="button primary" disabled={locked || !s.module} onClick={() => void s.publishModule()}>
              发布固定版本
            </button>
          </div>
          {s.cards.length === 0 && (
            <p className="hint">先到研究工作流里保存或运行卡片，再回来发布。</p>
          )}
        </div>

        <div className="lp-block">
          <div className="lp-block-head"><b>已发布的模块（{s.library.length}）</b></div>
          {s.library.length === 0 ? (
            <p className="lp-empty">{s.loading ? '正在读取模块库…' : '模块库还空着。发布固定版本后，可以在其他项目里导入。'}</p>
          ) : (
            <div className="lp-list">
              {s.library.map((item) => (
                <div className="lp-list-row" key={item.id}>
                  <div className="lp-list-main">
                    <b>{item.title}</b>
                    <small>{item.kind}@{item.version} · 指纹 {item.id.slice(0, 10)}</small>
                  </div>
                  <button type="button" className="button small" disabled={locked} onClick={() => void s.importModule(item.id)}>
                    导入当前项目
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </Section>
  )
}
