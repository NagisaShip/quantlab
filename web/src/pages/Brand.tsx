import { useState } from 'react'
import { BOT_FLASHES, BOT_STATES, BOT_VIEWS, QuantBot, QuantEyes, useBotFlash, type BotState, type BotView } from '../components/QuantBot'
import { Heading } from '../components/ui'
import type { PageProps } from './types'

/**
 * 设置 · 视觉规范：QuantLab Bot 的造型、表情语法、观点色与使用边界。
 * 所有页面共享这一页的规范；新页面用 Bot 前先对照这里。
 */
const SCENES: { title: string; state: BotState; view?: BotView; text: string }[] = [
  { title: '聊天界面', state: 'thinking', text: '回答前思考；完成时轻弹；报错出现 ×。问 AI 面板、AI 研究室的「处理中」都用它。' },
  { title: '量化工具', state: 'focus', text: '回测、训练、仿真时专注；多轮迭代冲刺时 > <；任务完成的提示条里轻弹。' },
  { title: '行情与模型', state: 'idle', view: 'bull', text: '只在有真实模型信号时表达看涨 / 看跌（模型库「最新预测」）；没有信号就保持白色。' },
  { title: '全局交互', state: 'idle', text: '品牌标识、加载、忙碌遮罩、空状态、提示条。待机时偶尔眨眼，体现生命感。' },
]

export default function BrandPage(_: PageProps) {
  const [state, setState] = useState<BotState>('thinking')
  const [view, setView] = useState<BotView>('neutral')
  const fx = useBotFlash()
  return (
    <>
      <Heading title="视觉规范" description="一个形状，多种状态：同一双 K 线眼睛，贯穿整个产品。" />

      <div className="brand-hero anim-rise">
        <QuantBot size={112} state={state} view={view} flash={fx.flash} />
        <div>
          <h2>QuantLab Bot</h2>
          <p>几何化的蓝色机器人头部，两根白色 K 线就是它的眼睛。基础造型永远不变，只有<b>状态</b>（动画）和<b>观点</b>（颜色）在变；两者相互独立——Bot 可以一边思考，一边表达看涨。</p>
          <div className="button-row" style={{ marginTop: 12, flexWrap: 'wrap' }}>
            {BOT_STATES.map((s) => (
              <button key={s.id} type="button" className={`chip${state === s.id ? ' on' : ''}`} onClick={() => setState(s.id)}>{s.label}</button>
            ))}
          </div>
          <div className="button-row" style={{ marginTop: 6, flexWrap: 'wrap' }}>
            {BOT_VIEWS.map((v) => (
              <button key={v.id} type="button" className={`chip${view === v.id ? ' on' : ''}`} onClick={() => setView(v.id)}>{v.label}</button>
            ))}
          </div>
        </div>
      </div>

      <section className="set-section">
        <div className="set-section-head"><h3>三种形态，同一双眼睛</h3></div>
        <div className="brand-grid">
          <div className="brand-cell"><QuantBot size={56} state="idle" /><b>完整头像</b><small>品牌标识、对话头像、忙碌遮罩、规范页</small><code>&lt;QuantBot&gt;</code></div>
          <div className="brand-cell"><span className="brand-eyes"><QuantEyes size={48} state="thinking" /></span><b>纯眼睛符号</b><small>加载、状态指示、按钮反馈等小型组件；颜色跟随文字</small><code>&lt;QuantEyes&gt;</code></div>
          <div className="brand-cell"><QuantBot size={56} variant="simple" motion="none" /><b>简化静态</b><small>浏览器标签页、桌面图标、≤18px 的小头像：平涂、无面罩、不动</small><code>variant="simple"</code></div>
          <div className="brand-cell"><QuantBot size={56} state="confused" motion="low" /><b>低动态</b><small>通知、空状态、错误页：只保留眨眼和一次性反馈</small><code>motion="low"</code></div>
        </div>
      </section>

      <section className="set-section">
        <div className="set-section-head"><h3>短动画（一次性）</h3></div>
        <p className="set-section-note">按钮点击、任务启动、完成反馈都复用眼睛的短动画；点一下按钮看上面的大 Bot。</p>
        <div className="button-row" style={{ flexWrap: 'wrap' }}>
          {BOT_FLASHES.map((f) => (
            <button key={f.id} type="button" className="button small" title={f.hint} onClick={() => fx.fire(f.id)}>
              <QuantEyes size={14} motion="none" />{f.label}
            </button>
          ))}
        </div>
      </section>

      <section className="set-section">
        <div className="set-section-head"><h3>表情语法</h3></div>
        <p className="set-section-note">让两根眼睛成为统一的表达语言，而不是为每种状态单独画图。</p>
        <div className="brand-grid stagger">
          {BOT_STATES.map((s) => (
            <div key={s.id} className="brand-cell">
              <QuantBot size={56} state={s.id} />
              <b>{s.label}</b>
              <small>{s.hint}</small>
              <code>state="{s.id}"</code>
            </div>
          ))}
        </div>
      </section>

      <section className="set-section">
        <div className="set-section-head"><h3>市场观点（与状态独立）</h3></div>
        <p className="set-section-note">
          看涨 / 看跌只填眼睛的实体，颜色跟随顶栏「红涨绿跌 / 绿涨红跌」的全局设置；必须对应真实的模型信号，不能用来表达普通情绪。
        </p>
        <div className="brand-matrix" style={{ gridTemplateColumns: `90px repeat(${BOT_VIEWS.length}, 1fr)` }}>
          <div />
          {BOT_VIEWS.map((v) => <div key={v.id} className="ch">{v.label}</div>)}
          {(['idle', 'thinking', 'focus', 'happy'] as BotState[]).map((s) => (
            <div key={s} style={{ display: 'contents' }}>
              <div className="rh">{BOT_STATES.find((x) => x.id === s)?.label}</div>
              {BOT_VIEWS.map((v) => <div key={v.id}><QuantBot size={40} state={s} view={v.id} /></div>)}
            </div>
          ))}
        </div>
      </section>

      <section className="set-section">
        <div className="set-section-head"><h3>全平台场景</h3></div>
        <div className="brand-grid">
          {SCENES.map((c) => (
            <div key={c.title} className="brand-cell">
              <QuantBot size={44} state={c.state} view={c.view} />
              <b>{c.title}</b>
              <small>{c.text}</small>
            </div>
          ))}
        </div>
      </section>

      <section className="set-section">
        <div className="set-section-head"><h3>尺寸</h3></div>
        <p className="set-section-note">最小 16px（表格行内、提示条），常用 20–24px（标识、对话），加载 40px，展示 52px 以上。同一场景内尺寸保持一致。</p>
        <div className="brand-sizes">
          {[16, 20, 24, 32, 40, 56, 80].map((n) => (
            <figure key={n}><QuantBot size={n} state="idle" /><figcaption>{n}px</figcaption></figure>
          ))}
        </div>
      </section>

      <section className="set-section">
        <div className="set-section-head"><h3>设计边界</h3></div>
        <div className="brand-rules">
          <div><b>克制</b>不加腮红、表情符号或装饰，让眼睛自己说话。嘴只有两种：完成的白色 √（随后回到待机）和报错的橙色 ×。</div>
          <div><b>明确</b>红绿只代表模型的市场观点，并且来自真实信号；情绪用动画表达，不用颜色。</div>
          <div><b>一致</b>所有页面都用同一个组件（QuantBot），不另画机器人。不同场景只换动画节奏，不换造型。</div>
          <div><b>可读</b>动画只动 transform 与透明度；系统开启「减少动态效果」时全部静止，含义仍能从文字读出。</div>
        </div>
      </section>
    </>
  )
}
