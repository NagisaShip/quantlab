/**
 * QuantLab Bot 的统一身份：名称、图标与固定语气文案。
 *
 * 临时问答（AskAI）、AI 研究室页头与空状态共用这里，避免出现「问 AI」「助手」
 * 等第二套 persona。造型与交互状态仍由 QuantBot.tsx 提供，这里只做组合与文案，
 * 不新增机器人形象。
 */
import { QuantBot, type BotState } from './QuantBot'

/** 平台唯一的 Bot 名称：页头、空状态、提示语都用它，不再写「问 AI」。 */
export const BOT_NAME = 'QuantLab Bot'
/** 悬停说明：一句话说清它是谁。 */
export const BOT_TITLE = 'QuantLab Bot · 研究助手'

export const BOT_VOICE = {
  /** 临时问答的轻量标识：只在当前页面保留，不写研究记录。 */
  ephemeral: '临时问答 · 不保存',
  ephemeralHint: '临时问答只保留在当前页面，不会写入研究记录；需要留存时升级为研究会话。',
  /** 升级入口与结果提示。 */
  upgrade: '升级为研究会话',
  upgradeHint: '把这段问答、页面资料与研究上下文保存成一条研究会话',
  upgrading: '升级中…',
  upgraded: '已升级为研究会话',
  upgradeFailed: '升级失败，会话仍保留在这里',
  /** 上下文条上的一句话说明：有研究上下文时都可以升级。 */
  upgradeBanner: '这段问答可以保存成研究会话',
  /** 已升级的会话在页头显示的来源标签。 */
  upgradedTag: '从问答升级',
  /** 服务端判断这次问题适合升级时的建议卡标题（窄面板用短版）。 */
  suggestTitle: '这次问题适合升级为研究会话',
  suggestTitleShort: '适合升级为研究会话',
  /** 研究室空状态的一句话。 */
  emptyLab: 'QuantLab Bot 已就位 · 输入一句想法开始研究，或输入 / 查看快捷指令',
} as const

/** Bot 图标 + 名称（可选）：页头与空状态的统一写法。 */export function BotMark({ size = 20, state = 'idle', label = BOT_NAME, showName = true, className }: {
  size?: number
  state?: BotState
  label?: string
  showName?: boolean
  className?: string
}) {
  return (
    <span className={`bot-mark${className ? ' ' + className : ''}`}>
      <QuantBot size={size} state={state} title={label} />
      {showName && <b className="bot-mark-name">{label}</b>}
    </span>
  )
}

/** 「临时问答 · 不保存」标识：Ask 头部专用，强调不会生成研究记录。 */
export function EphemeralChip({ className }: { className?: string }) {
  return <span className={`bot-ephemeral${className ? ' ' + className : ''}`} title={BOT_VOICE.ephemeralHint}>{BOT_VOICE.ephemeral}</span>
}

/**
 * 升级成功后的一次性提示：Ask 面板写入，目标会话页读取并清掉。
 * 用 sessionStorage 而不是全局状态，跨 hash 跳转后仍能提示一次。
 */
export const upgradeFlashKey = (projectId: string) => `qa.lab.flash.${projectId}`

