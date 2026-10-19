/**
 * QuantLab Bot：平台统一的视觉形象与交互语言。
 *
 * 一个形状，多种状态：几何化的蓝色机器人头部 + 两根 K 线做眼睛。基础造型永远不变，
 * 只有「状态」（动画、嘴）和「观点」（颜色）在变——
 *
 *   state（交互 / 情绪，互斥）               view（市场观点，独立于 state）
 *   idle      待机：安静，5–8 秒眨一次眼        neutral  中性：白色实心
 *   thinking  思考：两眼上下交错浮动            unknown  未知：实体变空心
 *   focus     专注：实体高频微幅呼吸            bull     看涨：实体与影线填「上涨色」
 *   strain    全力：眼睛变成 > <                bear     看跌：实体与影线填「下跌色」
 *   happy     完成：两眼同步弹动，嘴变成白色 √，随后自动回到待机
 *   confused  困惑：两眼错位，停顿
 *   error     报错：嘴变成橙色 ×
 *
 * 三种形态，同一套眼睛：
 * - QuantBot（完整头像）：品牌标识、对话头像、忙碌遮罩、规范页；
 * - QuantEyes（纯眼睛符号）：加载、状态指示、按钮反馈等小型组件；
 * - variant="simple"（简化静态）：浏览器标签页、桌面图标、≤ 18px 的小头像——
 *   去掉渐变与面罩，不播放动画。
 *
 * 规则（见「设置 · 视觉规范」）：
 * - 克制：只有完成的 √ 和报错的 × 两种嘴，不加别的表情装饰，让眼睛说话；
 * - 明确：bull / bear 只能来自真实模型信号，颜色跟随全局「红涨绿跌 / 绿涨红跌」（--up / --down）；
 * - 一致：所有页面都用这几个组件，不另画机器人；场景只换节奏（speed / motion），不换造型。
 *
 * 策略广场的社交表情见 Qmoji.tsx：复用同一个头部与 K 线眼睛，只扩展眼睛几何与短动画，
 * 不改变这里的交互状态语法，也不把涨跌红绿用于社交情绪。
 */
import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'

export type BotState = 'idle' | 'thinking' | 'focus' | 'strain' | 'happy' | 'confused' | 'error'
export type BotView = 'neutral' | 'unknown' | 'bull' | 'bear'
/** 一次性的眼睛短动画：按钮点击、任务启动、完成反馈 */
export type BotFlash = 'tap' | 'start' | 'done' | 'fail'
export type BotGesture = 'none' | 'wave' | 'point-left' | 'point-right' | 'think' | 'clap' | 'shrug'
/** full：完整动画；low：只保留待机眨眼与一次性反馈（通知、空状态、错误页）；none：完全静态 */
export type BotMotion = 'full' | 'low' | 'none'

export const BOT_STATES: { id: BotState; label: string; hint: string }[] = [
  { id: 'idle', label: '待机', hint: '安静；每 5–8 秒两根 K 线收拢成十字星「眨一下眼」' },
  { id: 'thinking', label: '思考', hint: '两眼上下交错浮动：模型在推理、生成' },
  { id: 'focus', label: '专注', hint: '实体高频微幅呼吸，像盘口 Tick 跳动：回测、训练、运行中' },
  { id: 'strain', label: '全力', hint: '眼睛变成 > <：重计算、长任务的冲刺' },
  { id: 'happy', label: '完成', hint: '两眼同步轻弹，嘴变成白色 √，随后回到待机' },
  { id: 'confused', label: '困惑', hint: '两眼错位并停顿：没理解、缺信息、需要你确认' },
  { id: 'error', label: '报错', hint: '嘴变成橙色 ×：失败、异常' },
]
export const BOT_VIEWS: { id: BotView; label: string; hint: string }[] = [
  { id: 'neutral', label: '中性', hint: '白色实心，不表达方向' },
  { id: 'unknown', label: '未知', hint: '实体变空心：还没有信号或信号不可用' },
  { id: 'bull', label: '看涨', hint: '实体与影线填上涨色（随全局红涨绿跌设置）' },
  { id: 'bear', label: '看跌', hint: '实体与影线填下跌色（随全局红涨绿跌设置）' },
]
export const BOT_FLASHES: { id: BotFlash; label: string; hint: string }[] = [
  { id: 'tap', label: '点击', hint: '眨一下：按钮按下' },
  { id: 'start', label: '启动', hint: '两眼抬起拉长：任务开始' },
  { id: 'done', label: '完成', hint: '同步轻弹 + √' },
  { id: 'fail', label: '失败', hint: '左右一晃 + 橙色 ×' },
]

/**
 * 把真实模型的预测值换成观点（只用于模型信号，不用于情绪）：
 * - 分类（涨跌方向、三重障碍）：上涨概率 > 0.55 看涨、< 0.45 看跌，中间中性；
 * - 截面收益排名（0~1）：> 0.6 看涨、< 0.4 看跌（相对强弱）；
 * - 未来收益：按符号；
 * - 波动率等没有方向的预测：一律中性；没有值：未知。
 */
export function viewFromSignal(value: number | null | undefined, opts: { task?: string; label?: string } = {}): BotView {
  if (value === null || value === undefined || !Number.isFinite(value)) return 'unknown'
  if (opts.label === 'volatility') return 'neutral'
  if (opts.task === 'clf' || opts.task === 'classification' || opts.label === 'direction' || opts.label === 'triple_barrier') {
    return value > 0.55 ? 'bull' : value < 0.45 ? 'bear' : 'neutral'
  }
  if (opts.label === 'cs_rank') return value > 0.6 ? 'bull' : value < 0.4 ? 'bear' : 'neutral'
  if (Math.abs(value) < 1e-9) return 'neutral'
  return value > 0 ? 'bull' : 'bear'
}

/** 一次性短动画：fire('tap' | 'start' | 'done' | 'fail') 后返回给组件的 flash 键会变，组件播一次。 */
export function useBotFlash() {
  const [flash, setFlash] = useState<{ kind: BotFlash; n: number } | null>(null)
  const fire = useCallback((kind: BotFlash) => setFlash((f) => ({ kind, n: (f?.n ?? 0) + 1 })), [])
  return { flash, fire }
}

const FLASH_MS: Record<BotFlash, number> = { tap: 260, start: 620, done: 1500, fail: 1300 }

/** 内部：管理眨眼、完成后回落、一次性短动画。返回最终渲染用的 state 与额外 class。 */
function useBotLife(state: BotState, motion: BotMotion, flash?: { kind: BotFlash; n: number } | null) {
  const [blink, setBlink] = useState(false)
  const [settled, setSettled] = useState(false)
  const [flashing, setFlashing] = useState<BotFlash | null>(null)
  const timer = useRef<number>()

  // 完成：√ 停留一会儿，然后回到待机
  useEffect(() => {
    setSettled(false)
    if (state !== 'happy') return
    const t = window.setTimeout(() => setSettled(true), 1800)
    return () => window.clearTimeout(t)
  }, [state])

  const effective: BotState = state === 'happy' && settled ? 'idle' : state

  // 待机眨眼：每 5–8 秒随机一次，两根实体瞬间收成十字星再恢复
  useEffect(() => {
    if (motion === 'none' || effective !== 'idle') return
    let alive = true
    const schedule = () => {
      timer.current = window.setTimeout(() => {
        if (!alive) return
        setBlink(true)
        window.setTimeout(() => { if (alive) setBlink(false) }, 170)
        schedule()
      }, 5000 + Math.random() * 3000)
    }
    schedule()
    return () => { alive = false; window.clearTimeout(timer.current) }
  }, [effective, motion])

  useEffect(() => {
    if (!flash || motion === 'none') return
    setFlashing(flash.kind)
    const t = window.setTimeout(() => setFlashing(null), FLASH_MS[flash.kind])
    return () => window.clearTimeout(t)
  }, [flash?.n, flash?.kind, motion]) // eslint-disable-line react-hooks/exhaustive-deps

  const mouth: 'check' | 'cross' | null =
    flashing === 'done' ? 'check' : flashing === 'fail' ? 'cross' : effective === 'happy' ? 'check' : effective === 'error' ? 'cross' : null
  // 低动态：持续的循环动画（思考 / 专注 / 全力 / 困惑）降为静态造型，只保留眨眼和一次性反馈
  const loop = motion === 'full' ? '' : ' calm'
  const extra = `${blink ? ' blink' : ''}${flashing ? ` flash-${flashing}` : ''}${loop}${motion === 'none' ? ' still' : ''}`
  return { effective, mouth, extra }
}

const labelOf = (state: BotState, view: BotView) =>
  `QuantLab Bot · ${BOT_STATES.find((s) => s.id === state)?.label ?? ''}${view !== 'neutral' ? ` · ${BOT_VIEWS.find((v) => v.id === view)?.label}` : ''}`

/** 两根 K 线眼睛（完整头像与纯眼睛符号共用）。坐标系 24×24。 */
function Eyes() {
  return (
    <g className="qbot-eyes">
      <g className="qbot-eye l">
        <path className="qbot-wick" d="M8.6 6.9v8.6" />
        <rect className="qbot-body" x="6.7" y="8.8" width="3.8" height="4.8" rx=".8" />
        <path className="qbot-squint" d="M7.2 8.9 10 11.2 7.2 13.5" />
      </g>
      <g className="qbot-eye r">
        <path className="qbot-wick" d="M15.4 6.9v8.6" />
        <rect className="qbot-body" x="13.5" y="9.5" width="3.8" height="3.6" rx=".8" />
        <path className="qbot-squint" d="M16.8 8.9 14 11.2 16.8 13.5" />
      </g>
    </g>
  )
}

function Mouth({ kind, y }: { kind: 'check' | 'cross' | null; y: number }) {
  return (
    <>
      <path className={`qbot-mouth check${kind === 'check' ? ' on' : ''}`} d={`M10.4 ${y + 1.1}l1.1 1.1 2.2-2.3`} />
      <path className={`qbot-mouth cross${kind === 'cross' ? ' on' : ''}`} d={`M11 ${y}l2 2M13 ${y}l-2 2`} />
    </>
  )
}

export function QuantBot({ size = 24, state = 'idle', view = 'neutral', title, className, speed = 1, still, motion, flash, variant, hands, interactive, gesture = 'none', entrance, onActivate, onHoverChange }: {
  size?: number
  state?: BotState
  view?: BotView
  title?: string
  className?: string
  /** 动画节奏：聊天里 1，加载里可以快一点，背景装饰慢一点；只换节奏，不换造型 */
  speed?: number
  /** 兼容旧写法：等同 motion="none" */
  still?: boolean
  motion?: BotMotion
  flash?: { kind: BotFlash; n: number } | null
  /** simple：简化静态版（图标、≤18px 小头像）；默认按尺寸自动选择 */
  variant?: 'full' | 'simple'
  /** Tutor 形态的两只白色圆角手；品牌图标默认不显示 */
  hands?: boolean
  /** 可点击：点击播放 tap 并回调 onActivate */
  interactive?: boolean
  gesture?: BotGesture
  /** 首次出现时弹跳拉伸入场 */
  entrance?: boolean
  onActivate?: () => void
  onHoverChange?: (hovering: boolean) => void
}) {
  const uid = useId().replace(/:/g, '')
  const [tapN, setTapN] = useState(0)
  const [hover, setHover] = useState(false)
  const simple = variant ? variant === 'simple' : size <= 18
  const m: BotMotion = still ? 'none' : motion ?? (simple ? 'low' : 'full')
  const activeFlash = flash ?? (tapN ? { kind: 'tap' as BotFlash, n: tapN } : null)
  const { effective, mouth, extra } = useBotLife(state, m, activeFlash)
  const label = title ?? labelOf(effective, view)
  const gestureClass = hands && gesture !== 'none' ? ` qbot-gesture-${gesture}` : ''
  const activate = () => {
    if (!interactive) return
    setTapN((n) => n + 1)
    onActivate?.()
  }
  const interaction = interactive ? {
    role: 'button' as const,
    tabIndex: 0,
    'aria-label': `${label}，点击与我互动`,
    onClick: activate,
    onKeyDown: (e: ReactKeyboardEvent<SVGSVGElement>) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate() }
    },
    onMouseEnter: () => { setHover(true); onHoverChange?.(true) },
    onMouseLeave: () => { setHover(false); onHoverChange?.(false) },
  } : { role: 'img' as const, 'aria-label': label }
  return (
    <svg className={`qbot qbot-${effective} qbot-${view}${simple ? ' simple' : ''}${hands ? ' qbot-hands' : ''}${interactive ? ' qbot-interactive' : ''}${hover ? ' qbot-hover' : ''}${entrance ? ' qbot-enter' : ''}${gestureClass}${extra}${className ? ' ' + className : ''}`}
      width={size} height={size} viewBox="0 0 24 24" {...interaction}
      style={{ ['--qb-speed' as string]: String(1 / Math.max(0.25, speed)), cursor: interactive ? 'pointer' : undefined }}>
      <title>{label}</title>
      {!simple && (
        <defs>
          <linearGradient id={`qbh-${uid}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="var(--qb-head-hi)" />
            <stop offset="1" stopColor="var(--qb-head)" />
          </linearGradient>
        </defs>
      )}
      <g className="qbot-head">
        <rect x="2.5" y="3.25" width="19" height="17.5" rx="5.2" fill={simple ? 'var(--qb-head)' : `url(#qbh-${uid})`} />
        {!simple && <rect className="qbot-visor" x="4.6" y="5.85" width="14.8" height="10.6" rx="3.4" />}
      </g>
      {!simple && hands && (
        <g className="qbot-hands" aria-hidden="true">
          <line className="qbot-hand qbot-hand--l" x1="3.4" y1="15.1" x2="1.0" y2="16.8" />
          <line className="qbot-hand qbot-hand--r" x1="20.6" y1="15.1" x2="23.0" y2="16.8" />
        </g>
      )}
      <Eyes />
      <Mouth kind={mouth} y={16.9} />
    </svg>
  )
}

/** 纯眼睛符号：加载、状态指示、按钮反馈等小型组件。颜色跟随文字（currentColor），观点色与完整头像一致。 */
export function QuantEyes({ size = 16, state = 'idle', view = 'neutral', title, className, speed = 1, motion, flash }: {
  size?: number
  state?: BotState
  view?: BotView
  title?: string
  className?: string
  speed?: number
  motion?: BotMotion
  flash?: { kind: BotFlash; n: number } | null
}) {
  const { effective, mouth, extra } = useBotLife(state, motion ?? 'full', flash)
  const label = title ?? labelOf(effective, view)
  return (
    <svg className={`qbot qeyes qbot-${effective} qbot-${view}${extra}${className ? ' ' + className : ''}`}
      width={size} height={size} viewBox="5 5 14 14" role="img" aria-label={label}
      style={{ ['--qb-speed' as string]: String(1 / Math.max(0.25, speed)) }}>
      <title>{label}</title>
      <Eyes />
      <Mouth kind={mouth} y={15.6} />
    </svg>
  )
}

/** 品牌字标：Bot + QuantLab。 */
export function BrandMark({ size = 20, state = 'idle', label = 'QuantLab' }: { size?: number; state?: BotState; label?: string }) {
  return <span className="brandmark"><QuantBot size={size} state={state} title="QuantLab" />{label && <span>{label}</span>}</span>
}
