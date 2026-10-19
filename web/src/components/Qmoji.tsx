/**
 * QuantLab Qmoji：策略广场发帖 / 评论专用的社交表情。
 *
 * 造型与 QuantBot 完全一致：蓝色几何头部 + 两根 K 线眼睛。没有腮红、道具或
 * 第三方 emoji 贴图；情绪只通过眼睛几何、头部姿态与短动画表达。量化分类会在
 * 面罩里出现极小的 HUD 字符（α、↗、01 等），它们是界面语言，不是贴纸。
 *
 * 正文里用 `:qbot-agree:` 这样的短码保存，渲染时替换成矢量表情；复制到剪贴板
 * 后仍然是可读的短码。涨跌红绿不参与社交情绪，仍只留给真实模型信号。
 */
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { lang } from '../i18n'

export type QmojiId =
  | 'hi' | 'agree' | 'laugh' | 'wow' | 'cry' | 'angry' | 'nervous' | 'love' | 'shy'
  | 'question' | 'think' | 'fire' | 'ok' | 'no' | 'sleep' | 'steady' | 'effort'
  | 'thanks' | 'respect' | 'shh' | 'vote' | 'party'
  | 'alpha' | 'rekt' | 'moon' | 'data'
  | 'overfit' | 'future' | 'wick' | 'drawdown'
export type QmojiGroup = 'mood' | 'status' | 'social' | 'quant'

export interface QmojiDef {
  id: QmojiId
  code: string
  name: string
  hint: string
  group: QmojiGroup
}

export const QMOJIS: QmojiDef[] = [
  { id: 'hi', code: ':qbot-hi:', name: '你好', hint: '歪头打招呼，像刚上线', group: 'mood' },
  { id: 'agree', code: ':qbot-agree:', name: '赞同', hint: '两根 K 线同时点头', group: 'mood' },
  { id: 'laugh', code: ':qbot-laugh:', name: '笑死', hint: '眼睛弯成上扬的 K 线', group: 'mood' },
  { id: 'wow', code: ':qbot-wow:', name: '惊了', hint: '眼睛拉长，头部后仰', group: 'mood' },
  { id: 'cry', code: ':qbot-cry:', name: '亏麻', hint: '眼睛下垂，一根影线变成泪', group: 'mood' },
  { id: 'angry', code: ':qbot-angry:', name: '生气', hint: '两眼向外压成锐角，头部轻颤', group: 'mood' },
  { id: 'nervous', code: ':qbot-nervous:', name: '紧张', hint: '眼睛缩小，影线变细，头部发抖', group: 'mood' },
  { id: 'love', code: ':qbot-love:', name: '喜欢', hint: '一只眼弯起，头部轻轻靠向你', group: 'mood' },
  { id: 'shy', code: ':qbot-shy:', name: '害羞', hint: '眼睛弯起，两颊淡淡粉色', group: 'mood' },

  { id: 'question', code: ':qbot-question:', name: '问号', hint: '一只眼空心，歪头看你', group: 'status' },
  { id: 'think', code: ':qbot-think:', name: '想想', hint: '两眼交错浮动，正在推理', group: 'status' },
  { id: 'fire', code: ':qbot-fire:', name: '冲', hint: '高频抖动与蓝色辉光', group: 'status' },
  { id: 'ok', code: ':qbot-ok:', name: '收到', hint: '白色 √ 只出现一次', group: 'status' },
  { id: 'no', code: ':qbot-no:', name: '不行', hint: '摇头，橙色 × 明确拒绝', group: 'status' },
  { id: 'sleep', code: ':qbot-sleep:', name: '摸鱼', hint: '闭眼慢呼吸，暂时离线', group: 'status' },
  { id: 'steady', code: ':qbot-steady:', name: '稳', hint: '水平 K 线，扫描线慢慢走过', group: 'status' },
  { id: 'effort', code: ':qbot-effort:', name: '努力', hint: '>< 眼 + 一滴汗，顶住', group: 'status' },

  { id: 'thanks', code: ':qbot-thanks:', name: '感谢', hint: '低头 + 白色 √，不多说', group: 'social' },
  { id: 'respect', code: ':qbot-respect:', name: '致敬', hint: '两眼下垂，慢慢点头', group: 'social' },
  { id: 'shh', code: ':qbot-shh:', name: '保密', hint: '一只眼闭成线，面罩里出现省略号', group: 'social' },
  { id: 'vote', code: ':qbot-vote:', name: '投票', hint: '眼睛一高一低，面罩里 0/1 切换', group: 'social' },
  { id: 'party', code: ':qbot-party:', name: '庆祝', hint: '两眼放大，头部弹跳，面罩里闪过 ✦', group: 'social' },

  { id: 'alpha', code: ':qbot-alpha:', name: '阿尔法', hint: '一只眼眯起，面罩里出现 α', group: 'quant' },
  { id: 'rekt', code: ':qbot-rekt:', name: '爆仓', hint: 'X 眼 + 低头，不加红绿', group: 'quant' },
  { id: 'moon', code: ':qbot-moon:', name: '起飞', hint: '两眼向上看，面罩里出现 ↗', group: 'quant' },
  { id: 'data', code: ':qbot-data:', name: '数据', hint: '眼睛专注，面罩里滚动 01', group: 'quant' },
  { id: 'overfit', code: ':qbot-overfit:', name: '过拟合', hint: '两根 K 线扭成 S 型，头部僵硬', group: 'quant' },
  { id: 'future', code: ':qbot-future:', name: '未来函数', hint: '一只眼鬼祟斜视，旁边闪过 »', group: 'quant' },
  { id: 'wick', code: ':qbot-wick:', name: '插针', hint: '实体压扁，下影线突然刺破面罩', group: 'quant' },
  { id: 'drawdown', code: ':qbot-drawdown:', name: '回撤', hint: '两眼阶梯下坠，面罩闪过 ↘', group: 'quant' },
]

export const QUICK_QMOJI: QmojiId[] = ['agree', 'respect', 'alpha', 'question', 'cry']

export const QMOJI_GROUPS: { id: QmojiGroup; label: string }[] = [
  { id: 'mood', label: '情绪' },
  { id: 'status', label: '状态' },
  { id: 'social', label: '社交' },
  { id: 'quant', label: '量化' },
]

const EN_NAMES: Record<QmojiId, string> = {
  hi: 'Hello', agree: 'Agree', laugh: 'LOL', wow: 'Wow', cry: 'Ouch', angry: 'Angry', nervous: 'Nervous', love: 'Love', shy: 'Shy',
  question: 'Huh?', think: 'Thinking', fire: 'Go', ok: 'Got it', no: 'Nope', sleep: 'Away', steady: 'Steady', effort: 'Effort',
  thanks: 'Thanks', respect: 'Respect', shh: 'Shh', vote: 'Vote', party: 'Party',
  alpha: 'Alpha', rekt: 'Rekt', moon: 'Moon', data: 'Data',
  overfit: 'Overfit', future: 'Lookahead', wick: 'Wick', drawdown: 'Drawdown',
}

const BY_ID = new Map(QMOJIS.map((q) => [q.id, q]))
const BY_CODE = new Map(QMOJIS.map((q) => [q.code, q]))
// 兼容平台视觉规范里的 strain 状态名：:qbot-strain: 与 :qbot-effort: 指向同一个努力表情
const _effort = BY_ID.get('effort')
if (_effort) BY_CODE.set(':qbot-strain:', _effort)
export const qmojiById = (id: string) => BY_ID.get(id as QmojiId)
export const qmojiByCode = (code: string) => BY_CODE.get(code)
export const qmojiName = (id: QmojiId) => (lang === 'en' ? (EN_NAMES[id] ?? id) : (BY_ID.get(id)?.name ?? id))

const E = (s: string) => s.split(/\s+/).filter(Boolean)
export type NativeCat = 'common' | 'smileys' | 'gestures' | 'symbols' | 'market' | 'objects'
export interface NativeGroup { id: NativeCat; label: string; items: string[] }

/** 原生 Unicode emoji：按「常用 / 笑脸 / 手势 / 符号 / 行情 / 物件」分类。 */
export const NATIVE_GROUPS: NativeGroup[] = [
  { id: 'common', label: '常用', items: E('👍 👏 🎉 😂 🤔 😮 😭 🔥 🚀 📈 📉 ✅ ❌ 💡 ❤️ 🙏') },
  { id: 'smileys', label: '笑脸', items: E('😀 😃 😄 😁 😆 😅 😂 🤣 😊 😇 🙂 🙃 😉 😌 😍 🥰 😘 😜 🤪 🤨 🧐 🤓 😎 🥳 😏 😒 😞 😔 😟 😕 🙁 😣 😖 😫 😩 🥺 😢 😭 😤 😠 😡 🤬 🤯 😳 🥵 🥶 😱 😨 😰 😥 😓 🤗 🤔 🤭 🤫 🤥 😶 😐 😑 😬 🙄 😯 😦 😧 😮 😲 🥱 😴 🤤 😪 😵 🤐 🥴 🤢 🤮 🤧 😷 🤒 🤕 🤑 🤠 😈 👿 👻 💀') },
  { id: 'gestures', label: '手势', items: E('👍 👎 👌 ✌️ 🤞 🤟 🤘 🤙 👈 👉 👆 👇 ☝️ ✋ 🤚 🖐️ 🖖 👋 🤝 🙏 💪 👏 🙌 👐 🤲 🤜 🤛 ✊ 👊 🫡 🫣 🫠') },
  { id: 'symbols', label: '符号', items: E('❤️ 🧡 💛 💚 💙 💜 🖤 🤍 💔 ❣️ 💕 💞 💓 💗 💖 💘 💝 💯 ✅ ❌ ⚠️ ❓ ❗ 💡 🔥 ✨ ⭐ 🌟 💫 ⚡ 💥 💦 💨 🎯 🎉 🎊 🏆 🥇 🥈 🥉') },
  { id: 'market', label: '行情', items: E('📈 📉 📊 💹 🪙 💰 💵 💴 💶 💷 🏦 🧾 🧮 ⚖️ 🛡️ 🎲 🧠 🤖 🔍 🧪 ⏱️ ⌛ 🔄 ⏫ ⏬ ➡️ ⬅️ 🔔 🔕 📣 📢') },
  { id: 'objects', label: '物件', items: E('🚀 🛰️ 💻 🖥️ ⌨️ 🖱️ 📱 📟 🧑‍💻 👨‍💻 👩‍💻 📚 📝 📌 📎 🔗 🔒 🔓 🛠️ ⚙️ 🧰 📦 📁 📂 🗂️ 📅 🗓️ ⏰ ⏳ ☕ 🍵 🍺 🍻 🥂 🍕 🍜 🍱') },
]

const RECENT_KEY = 'qa.qmoji.recent'
function loadRecent(): string[] {
  try {
    if (typeof localStorage === 'undefined') return []
    const raw = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]')
    return Array.isArray(raw) ? raw.filter((x): x is string => typeof x === 'string').slice(0, 24) : []
  } catch { return [] }
}

function Glyph({ id }: { id: QmojiId }) {
  if (id === 'vote') {
    return (
      <g className="qm-glyph qm-glyph-vote" aria-hidden="true">
        <text className="v0" x="9.2" y="15.2" textAnchor="middle">0</text>
        <text className="v1" x="14.8" y="15.2" textAnchor="middle">1</text>
      </g>
    )
  }
  if (id === 'future') {
    return (
      <g className="qm-glyph qm-fast" aria-hidden="true">
        <path d="M17.9 9.3l1.1 1.2-1.1 1.2M16.6 9.3l1.1 1.2-1.1 1.2" />
      </g>
    )
  }
  const glyph = id === 'shh' ? '…' : id === 'party' ? '✦' : id === 'alpha' ? 'α' : id === 'moon' ? '↗' : id === 'data' ? '01' : id === 'drawdown' ? '↘' : ''
  return glyph
    ? <text className={`qm-glyph qm-glyph-${id}`} x="12" y="15.2" textAnchor="middle" aria-hidden="true">{glyph}</text>
    : null
}

function Mouth() {
  return (
    <g className="qm-mouth" aria-hidden="true">
      <path className="qm-check" d="M10.4 18l1.1 1.1 2.2-2.3" />
      <path className="qm-cross" d="M11 16.9l2 2M13 16.9l-2 2" />
    </g>
  )
}

function Eyes() {
  return (
    <g className="qm-eyes" aria-hidden="true">
      <g className="qm-eye l">
        <path className="qm-wick" d="M8.6 6.9v8.6" />
        <rect className="qm-body" x="6.7" y="8.8" width="3.8" height="4.8" rx=".8" />
        <path className="qm-chev" d="M6.9 12.6 8.7 10.6 10.5 12.6" />
        <path className="qm-arc" d="M6.9 12.2 Q8.7 9.6 10.5 12.2" />
        <path className="qm-droop" d="M6.9 10.4 Q8.7 13.2 10.5 10.4" />
        <path className="qm-line" d="M6.9 11.3h3.4" />
        <path className="qm-x" d="M7.1 9.2l2.9 2.9M10 9.2l-2.9 2.9" />
        <path className="qm-strain" d="M7.0 9.4 9.8 11.2 7.0 13.0" />
        <path className="qm-wave" d="M7 13.2 C7 10.8 10.4 11.4 10.4 9.2" />
        <path className="qm-long-wick" d="M8.6 9.2v9.8" />
      </g>
      <g className="qm-eye r">
        <path className="qm-wick" d="M15.4 6.9v8.6" />
        <rect className="qm-body" x="13.5" y="9.5" width="3.8" height="3.6" rx=".8" />
        <path className="qm-chev" d="M13.5 12.6 15.3 10.6 17.1 12.6" />
        <path className="qm-arc" d="M13.5 12.2 Q15.3 9.6 17.1 12.2" />
        <path className="qm-droop" d="M13.5 10.4 Q15.3 13.2 17.1 10.4" />
        <path className="qm-line" d="M13.7 11.3h3.4" />
        <path className="qm-x" d="M14.1 10.1l2.9 2.9M17 10.1l-2.9 2.9" />
        <path className="qm-strain" d="M17.0 9.4 14.2 11.2 17.0 13.0" />
        <path className="qm-wave" d="M13.4 13.4 C13.4 11 16.8 11.6 16.8 9.4" />
        <path className="qm-long-wick" d="M15.4 9.6v9.4" />
      </g>
    </g>
  )
}

export function Qmoji({ id, size = 24, animated = false, className, title }: {
  id: QmojiId
  size?: number
  /** true：循环播放；'hover'：悬停播放；'once'：页面中第一次出现时播放一次；false：纯静态 */
  animated?: boolean | 'hover' | 'once'
  className?: string
  title?: string
}) {
  const def = BY_ID.get(id)
  const uid = useId().replace(/[^a-zA-Z0-9]/g, '')
  const label = title ?? (lang === 'en' ? `QuantLab emoji: ${EN_NAMES[id] ?? id}` : `QuantLab 表情：${def?.name ?? id}（${def?.code ?? id}）`)
  const motion = animated === true ? ' qm-anim' : animated === 'hover' ? ' qm-hover' : animated === 'once' ? ' qm-once' : ''
  return (
    <span className={`qmoji qm-${id}${motion}${className ? ' ' + className : ''}`}
      role="img" aria-label={label} title={label} data-qmoji={id}>
      <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true" focusable="false">
        <defs>
          <linearGradient id={`qmj-${uid}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="var(--qb-head-hi)" />
            <stop offset="1" stopColor="var(--qb-head)" />
          </linearGradient>
        </defs>
        <g className="qm-head">
          <rect x="2.5" y="3.25" width="19" height="17.5" rx="5.2" fill={`url(#qmj-${uid})`} />
          <rect className="qm-visor" x="4.6" y="5.85" width="14.8" height="10.6" rx="3.4" />
          <rect className="qm-scan" x="4.9" y="9.05" width="2.1" height="1.2" rx=".6" />
        </g>
        <Glyph id={id} />
        <Eyes />
        <g className="qm-extras" aria-hidden="true">
          <path className="qm-sweat" d="M19.4 7.2 C19.4 8.1 18.7 8.6 18.7 9.3 C18.7 10.0 19.0 10.4 19.6 10.4 C20.2 10.4 20.5 10.0 20.5 9.3 C20.5 8.6 19.8 8.1 19.4 7.2 Z" />
          <rect className="qm-blush l" x="6.1" y="14.3" width="3.2" height="1.2" rx=".6" />
          <rect className="qm-blush r" x="14.7" y="14.3" width="3.2" height="1.2" rx=".6" />
        </g>
        <Mouth />
        {id === 'cry' && <path className="qm-tear" d="M8.6 13.4v5.8" />}
      </svg>
    </span>
  )
}

/** 表情面板：Qmoji + 原生 emoji，支持最近使用与分类快速切换。 */
export function QmojiPicker({ onPick, small, placement = 'down', label = '表情', title = '插入表情', onlyQmoji }: {
  onPick: (text: string) => void
  small?: boolean
  placement?: 'down' | 'up'
  label?: string
  title?: string
  /** 只展示 Qmoji，不展示原生 emoji 标签页（Reaction 栏使用） */
  onlyQmoji?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState<'qbot' | 'native'>('qbot')
  const [recent, setRecent] = useState<string[]>(loadRecent)
  const [qcat, setQcat] = useState<QmojiGroup | 'recent'>(() => loadRecent().some((x) => !!qmojiByCode(x)) ? 'recent' : 'mood')
  const [ncat, setNcat] = useState<NativeCat | 'recent'>(() => loadRecent().some((x) => !qmojiByCode(x)) ? 'recent' : 'common')
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null)
  const root = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const pop = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => {
      const t = e.target as Node
      if (!root.current?.contains(t) && !pop.current?.contains(t)) setOpen(false)
    }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', esc)
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', esc) }
  }, [open])

  useLayoutEffect(() => {
    if (!open) return
    const place = () => {
      const r = trigger.current?.getBoundingClientRect()
      if (!r) return
      const width = Math.min(344, window.innerWidth - 24)
      const height = pop.current?.offsetHeight || 400
      let left = Math.max(12, Math.min(r.left, window.innerWidth - width - 12))
      let top = placement === 'up' ? r.top - height - 6 : r.bottom + 6
      if (top < 12) top = r.bottom + 6
      if (top + height > window.innerHeight - 12) top = Math.max(12, r.top - height - 6)
      setPos({ left, top })
    }
    place()
    const onMove = () => place()
    window.addEventListener('resize', onMove)
    window.addEventListener('scroll', onMove, true)
    return () => { window.removeEventListener('resize', onMove); window.removeEventListener('scroll', onMove, true) }
  }, [open, placement, tab, qcat, ncat])

  const qRecent = recent.map(qmojiByCode).filter((q): q is QmojiDef => !!q)
  const nRecent = recent.filter((x) => !qmojiByCode(x))
  const qList = qcat === 'recent' ? qRecent : QMOJIS.filter((q) => q.group === qcat)
  const nList = ncat === 'recent' ? nRecent : (NATIVE_GROUPS.find((g) => g.id === ncat)?.items ?? [])

  const pick = (text: string) => {
    const next = [text, ...recent.filter((x) => x !== text)].slice(0, 24)
    setRecent(next)
    try { localStorage.setItem(RECENT_KEY, JSON.stringify(next)) } catch { /* 私密模式 */ }
    onPick(text)
    setOpen(false)
  }

  const qcats: { id: QmojiGroup | 'recent'; label: string }[] = [{ id: 'recent', label: '最近' }, ...QMOJI_GROUPS]
  const ncats: { id: NativeCat | 'recent'; label: string }[] = [{ id: 'recent', label: '最近' }, ...NATIVE_GROUPS.map((g) => ({ id: g.id, label: g.label }))]

  return (
    <div className={`qmoji-anchor${open ? ' open' : ''}`} ref={root}>
      <button ref={trigger} type="button" className={`rt-btn qmoji-trigger${small ? ' small' : ''}`} title={title} aria-label={title}
        aria-haspopup="dialog" aria-expanded={open} onMouseDown={(e) => e.preventDefault()} onClick={() => setOpen(!open)}>
        <Qmoji id="agree" size={16} animated={false} />
        {!small && <span>{label}</span>}
      </button>
      {open && createPortal(
        <div ref={pop} className={`qmoji-pop ${placement}`} role="dialog" aria-label={lang === 'en' ? (onlyQmoji ? 'Qmoji panel' : 'Emoji panel') : (onlyQmoji ? 'Qmoji 面板' : '表情面板')}
          style={{ left: pos?.left ?? -9999, top: pos?.top ?? -9999 }}>
          <div className="qmoji-pop-head">
            <div className="qmoji-tabs" role="tablist" aria-label={lang === 'en' ? 'Emoji type' : '表情类型'}>
              <button type="button" role="tab" aria-selected={tab === 'qbot'} className={tab === 'qbot' ? 'on' : ''}
                onMouseDown={(e) => e.preventDefault()} onClick={() => setTab('qbot')}>Qmoji</button>
              {!onlyQmoji && <button type="button" role="tab" aria-selected={tab === 'native'} className={tab === 'native' ? 'on' : ''}
                onMouseDown={(e) => e.preventDefault()} onClick={() => setTab('native')}>Emoji</button>}
            </div>
            <span className="qmoji-esc">Esc 关闭</span>
          </div>

          <div className="qmoji-cats" role="tablist" aria-label={tab === 'qbot' ? (lang === 'en' ? 'Qmoji categories' : 'Qmoji 分类') : (lang === 'en' ? 'Emoji categories' : 'Emoji 分类')}>
            {tab === 'qbot'
              ? qcats.map((c) => (
                <button key={c.id} type="button" role="tab" aria-selected={qcat === c.id} className={qcat === c.id ? 'on' : ''}
                  onMouseDown={(e) => e.preventDefault()} onClick={() => setQcat(c.id)}>{c.label}</button>
              ))
              : ncats.map((c) => (
                <button key={c.id} type="button" role="tab" aria-selected={ncat === c.id} className={ncat === c.id ? 'on' : ''}
                  onMouseDown={(e) => e.preventDefault()} onClick={() => setNcat(c.id)}>{c.label}</button>
              ))}
          </div>

          <div className="qmoji-content">
            {tab === 'qbot' ? (
              qList.length ? (
                <div className="qmoji-grid">
                  {qList.map((q) => (
                    <button key={q.id} type="button" className="qmoji-cell"
                      title={`${lang === 'en' ? EN_NAMES[q.id] : q.name} · ${q.code}`}
                      onMouseDown={(e) => e.preventDefault()} onClick={() => pick(q.code)}>
                      <Qmoji id={q.id} size={30} animated="hover" />
                      <span>{q.name}</span>
                    </button>
                  ))}
                </div>
              ) : <div className="qmoji-empty">{lang === 'en' ? 'Used Qmoji will appear here' : '用过的 Qmoji 会出现在这里'}</div>
            ) : (
              nList.length ? (
                <div className="qmoji-native">
                  {nList.map((e, i) => (
                    <button key={`${e}-${i}`} type="button" className="qmoji-native-cell" title={`${lang === 'en' ? 'Insert' : '插入'} ${e}`}
                      onMouseDown={(ev) => ev.preventDefault()} onClick={() => pick(e)}>{e}</button>
                  ))}
                </div>
              ) : <div className="qmoji-empty">{lang === 'en' ? 'Used emoji will appear here' : '用过的 emoji 会出现在这里'}</div>
            )}
          </div>

          <div className="qmoji-foot">
            {tab === 'qbot'
              ? <>输入短码也可以：<code>:qbot-agree:</code></>
              : <>{lang === 'en' ? 'Recently used emoji are remembered automatically' : '最近使用的 emoji 会自动记住'}</>}
          </div>
        </div>,
        document.body,
      )}
    </div>
  )
}
