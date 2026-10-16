/**
 * 设置 · 研究偏好：AI 研究室的默认行为、对话偏好、外观与首页关注列表，都存在本机浏览器。
 * 和对话框底栏、模型页里的开关读写同一组键，改一处处处生效。
 */
import { useState } from 'react'
import { LAB_PREF_KEYS, type Effort, type Permission } from '../../apiLab'
import { StepSlider } from '../../components/StepSlider'
import { getTheme, getUpDown, setTheme, setUpDown, store, type ThemePref, type UpDown } from '../../components/prefs'
import { RESEARCH_TYPES, type ResearchType } from '../lab/domains'
import { MarketSwitcher } from '../lab/research'
import type { PageProps } from '../types'

const PERMISSIONS: { id: Permission; label: string; hint: string }[] = [
  { id: 'auto', label: '自动', hint: 'AI 直接改代码、跑回测' },
  { id: 'accept_edits', label: '接受编辑', hint: '改代码直接生效，运行前先问' },
  { id: 'manual', label: '手动', hint: '改动与运行都先问你' },
  { id: 'plan', label: '计划', hint: '先出计划，你确认后再动手' },
]
const EFFORTS: { id: Effort; label: string; hint: string }[] = [
  { id: 'low', label: '低', hint: '快速回答' }, { id: 'medium', label: '中', hint: '大多数任务' },
  { id: 'high', label: '高', hint: '复杂策略与排错' }, { id: 'max', label: '极高', hint: '最深入，最慢' },
]

function Switch({ on, label, onChange }: { on: boolean; label: string; onChange: (v: boolean) => void }) {
  return <button type="button" role="switch" aria-checked={on} aria-label={label} className={`switch${on ? ' on' : ''}`} onClick={() => onChange(!on)}><i /></button>
}

export default function LabPrefsTab({ notify }: Pick<PageProps, 'notify'>) {
  const [rtype, setRtype] = useState(() => store.get('qa.lab.rtype', 'strategy') as ResearchType)
  const [perm, setPerm] = useState(() => store.get('qa.lab.permission', 'auto') as Permission)
  const [effort, setEffort] = useState(() => store.get('qa.lab.effort', 'medium') as Effort)
  const [suggest, setSuggest] = useState(() => store.get('qa.lab.suggest', '1') === '1')
  const [team, setTeam] = useState(() => store.get(LAB_PREF_KEYS.teamCtx, '1') === '1')
  const [maxTools, setMaxTools] = useState(() => store.get(LAB_PREF_KEYS.maxTools, '1000'))
  const [theme, setThemeState] = useState<ThemePref>(getTheme())
  const [updown, setUpDownState] = useState<UpDown>(getUpDown())
  const [watch, setWatch] = useState<string[]>(() => { try { const v = JSON.parse(store.get('qa.home.watch', '[]')); return Array.isArray(v) && v.length ? v : ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT'] } catch { return ['BTC/USDT'] } })
  const [watchTf, setWatchTf] = useState(() => store.get('qa.home.watchTf', '1h'))
  const saved = (msg: string) => notify(`已保存：${msg}`)

  return (
    <>
      <section className="set-section">
        <div className="set-section-head"><h3>新对话的默认设置</h3></div>
        <div className="set-row stack">
          <div><b>默认研究类型</b><small>打开 AI 研究室时滑块停在这里；完整流程会从选标的一路做到执行并出报告</small></div>
          <StepSlider options={RESEARCH_TYPES} value={rtype} label="默认研究类型" compact
            onChange={(t) => { setRtype(t); store.set('qa.lab.rtype', t); saved(`默认研究类型 ${RESEARCH_TYPES.find((x) => x.id === t)?.label}`) }} />
        </div>
        <div className="set-row stack">
          <div><b>权限模式</b><small>AI 改代码、跑回测之前要不要先问你</small></div>
          <div className="cmp-seg" role="group" aria-label="权限模式" style={{ gridTemplateColumns: `repeat(${PERMISSIONS.length}, 1fr)` }}>
            {PERMISSIONS.map((p) => (
              <button key={p.id} type="button" className={perm === p.id ? 'on' : ''} aria-pressed={perm === p.id} title={p.hint}
                onClick={() => { setPerm(p.id); store.set('qa.lab.permission', p.id); saved(`权限模式 ${p.label}`) }}>{p.label}</button>
            ))}
          </div>
          <small className="muted">{PERMISSIONS.find((p) => p.id === perm)?.hint}</small>
        </div>
        <div className="set-row stack">
          <div><b>思考努力程度</b><small>新对话默认的思考深度，对话里底栏随时可改</small></div>
          <StepSlider options={EFFORTS} value={effort} label="思考努力程度" compact
            onChange={(e) => { setEffort(e); store.set('qa.lab.effort', e); saved(`思考努力程度 ${EFFORTS.find((x) => x.id === e)?.label}`) }} />
        </div>
      </section>

      <section className="set-section">
        <div className="set-section-head"><h3>对话</h3></div>
        <div className="set-row">
          <div><b>AI 生成简短建议</b><small>每次回答末尾给出 2-3 条你可能想接着说的话</small></div>
          <Switch on={suggest} label="AI 生成简短建议" onChange={(v) => { setSuggest(v); store.set('qa.lab.suggest', v ? '1' : '0'); saved(v ? '开启 AI 建议' : '关闭 AI 建议') }} />
        </div>
        <div className="set-row">
          <div><b>团队讨论结论自动加入上下文</b><small>/discuss、/team 的汇总结论自动钉进对话上下文，之后每轮都带给模型</small></div>
          <Switch on={team} label="团队讨论结论自动加入上下文" onChange={(v) => { setTeam(v); store.set(LAB_PREF_KEYS.teamCtx, v ? '1' : '0'); saved(v ? '团队结论自动进上下文' : '团队结论不自动进上下文') }} />
        </div>
        <div className="set-row">
          <div><b>单轮工具调用上限</b><small>AI 一轮里最多调用多少次工具（查数据、回测、搜索、子 Agent…）。默认 1000，范围 1–5000</small></div>
          <input type="number" className="set-num" min={1} max={5000} step={50} value={maxTools} aria-label="单轮工具调用上限"
            onChange={(e) => setMaxTools(e.target.value)}
            onBlur={() => { const n = Math.max(1, Math.min(5000, Math.round(Number(maxTools) || 1000))); setMaxTools(String(n)); store.set(LAB_PREF_KEYS.maxTools, String(n)); saved(`单轮工具调用上限 ${n} 次`) }} />
        </div>
      </section>

      <section className="set-section">
        <div className="set-section-head"><h3>外观</h3></div>
        <div className="set-row">
          <div><b>涨跌配色</b><small>影响行情、收益与 Bot 的看涨 / 看跌颜色</small></div>
          <div className="cmp-seg" role="group" aria-label="涨跌配色" style={{ gridTemplateColumns: 'repeat(2, 1fr)', width: 200 }}>
            {(['red', 'green'] as UpDown[]).map((v) => (
              <button key={v} type="button" className={updown === v ? 'on' : ''} aria-pressed={updown === v}
                onClick={() => { setUpDown(v); setUpDownState(v); saved(v === 'red' ? '红涨绿跌' : '绿涨红跌') }}>{v === 'red' ? '红涨绿跌' : '绿涨红跌'}</button>
            ))}
          </div>
        </div>
        <div className="set-row">
          <div><b>主题</b><small>跟随系统时会随系统的深浅色切换</small></div>
          <div className="cmp-seg" role="group" aria-label="主题" style={{ gridTemplateColumns: 'repeat(3, 1fr)', width: 240 }}>
            {([['light', '浅色'], ['dark', '夜间'], ['system', '跟随系统']] as [ThemePref, string][]).map(([v, l]) => (
              <button key={v} type="button" className={theme === v ? 'on' : ''} aria-pressed={theme === v}
                onClick={() => { setTheme(v); setThemeState(v); saved(`主题 ${l}`) }}>{l}</button>
            ))}
          </div>
        </div>
      </section>

      <section className="set-section">
        <div className="set-section-head"><h3>首页关注列表</h3></div>
        <div className="set-row">
          <div><b>关注的标的与周期</b><small>{watch.join('、')} · {watchTf}。首页右侧显示，没有真实行情的标的会提示同步</small></div>
          <MarketSwitcher symbols={watch} timeframe={watchTf} multi notify={notify}
            onChange={(s, t) => { setWatch(s); setWatchTf(t); store.set('qa.home.watch', JSON.stringify(s)); store.set('qa.home.watchTf', t); saved('首页关注列表') }} />
        </div>
      </section>
      <DirectMarketRow saved={saved} />
    </>
  )
}

/** 展示用行情由浏览器直连交易所（关注列表、行情带、数据中心 K 线），服务器只取回测要用的数据，避免服务器 IP 被限流或封禁。 */
function DirectMarketRow({ saved }: { saved: (m: string) => void }) {
  const [on, setOn] = useState(() => store.get('qa.market.direct', '1') !== '0')
  return (
    <section className="set-section">
      <div className="set-section-head"><h3>行情请求</h3></div>
      <div className="set-row">
        <div><b>浏览器直连交易所</b><small>关注列表、行情带、数据中心的 K 线由你的浏览器直接向交易所公共接口请求，不经服务器转发，避免服务器 IP 被限流；直连失败时自动改走服务器。回测始终用服务器上可审计的数据</small></div>
        <button type="button" role="switch" aria-checked={on} aria-label="浏览器直连交易所" className={`switch${on ? ' on' : ''}`}
          onClick={() => { const v = !on; setOn(v); store.set('qa.market.direct', v ? '1' : '0'); saved(v ? '行情改由浏览器直连交易所' : '行情改由服务器代取') }}><i /></button>
      </div>
    </section>
  )
}
