import { useEffect, useState } from 'react'
import { Markdown } from '../components/markdown'
import { Heading } from '../components/ui'
import type { PageProps } from './types'

const SECTIONS: { id: string; title: string; md: string }[] = [
  {
    id: 'start',
    title: '快速开始',
    md: `一个策略就是一份 Python 文件。最小可运行的例子：

\`\`\`python
PARAMS = {"fast": 12, "slow": 48, "weight": 0.8}   # 可调参数（参数优化从这里读）

def initialize(ctx):
    set_benchmark(ctx.securities[0])               # 比较基准
    run_daily(rebalance, time="close")             # 每根 bar 收盘后运行

def rebalance(ctx):
    s = ctx.securities[0]
    closes = history(s, PARAMS["slow"], "close")   # 最近 N 根已收盘 bar
    if len(closes) < PARAMS["slow"]:
        return
    fast = sum(closes[-PARAMS["fast"]:]) / PARAMS["fast"]
    slow = sum(closes) / len(closes)
    order_target_percent(s, PARAMS["weight"] if fast > slow else 0)
\`\`\`

- 所有 SDK 函数**无需 import** 即可使用（写 \`from quant_agent.sdk import ...\` 也可以）。
- 在编辑器里 **Ctrl+Enter** 编译运行，**Ctrl+S** 保存；「运行回测」生成完整回测记录。
- 写法与聚宽基本一致：\`initialize / run_daily / history / order_target_percent / g / log / record\`。`,
  },
  {
    id: 'lifecycle',
    title: '生命周期与调度',
    md: `| 函数 | 说明 |
| --- | --- |
| \`initialize(ctx)\` | 启动时调用一次：设置基准、成本、注册定时任务、初始化 \`g\` |
| \`run_daily(func, time="close")\` | 每根 bar 收盘后运行 \`func(ctx)\`（time 可为 open / close / every_bar） |
| \`run_weekly(func, weekday=1)\` | 每周指定星期几（1=周一 … 7=周日），每个周期只触发一次 |
| \`run_monthly(func, monthday=1)\` | 每月指定日期（1~28），每个周期只触发一次 |
| \`handle_data(ctx)\` | 没有注册任何定时任务时，每根 bar 调用一次 |
| \`before_trading_start(ctx)\` | 第一根计分 bar 之前调用 |
| \`after_trading_end(ctx)\` | 最后一根 bar 之后调用 |

**预热**：前 N 根 bar（默认 100）只推进数据、不调用策略，所以策略在第一次运行时 \`history\` 已经能拿到完整窗口。`,
  },
  {
    id: 'data',
    title: '数据',
    md: `| 函数 | 返回 |
| --- | --- |
| \`history(security, count, "close")\` | \`list[float]\`，最近 count 根**已收盘** bar 的收盘价 |
| \`history(security, count, ["high", "low", "close"])\` | \`dict[str, list]\` |
| \`attribute_history(security, count, unit, fields)\` | 同上，兼容聚宽写法 |
| \`get_price(security, count, fields, end_dt=None)\` | end_dt 不允许晚于当前时刻 |
| \`get_current_data()\` | \`{security: {"last_price", "is_tradable"}}\` |
| \`get_security_info(security)\` | 基础信息（base / quote / type） |

可用字段：\`open\` \`high\` \`low\` \`close\` \`volume\` \`t\`（开盘时间）\`close_time\`。

**不存在未来数据**：任何时刻拿到的最后一根 bar，其收盘时间都 ≤ \`ctx.current_dt\`。`,
  },
  {
    id: 'trade',
    title: '交易',
    md: `所有下单函数只表达**目标**，成交价、手续费、滑点由平台撮合：

| 函数 | 含义 |
| --- | --- |
| \`order_target_percent(s, 0.8)\` | 把 s 的市值调到总权益的 80%；0 表示清仓（最常用） |
| \`order_target_value(s, 2000)\` | 把 s 的市值调到 2000 USDT |
| \`order_target(s, 0.5)\` | 把 s 的持仓数量调到 0.5 个币 |
| \`order_value(s, ±1000)\` | 相对加减 1000 USDT |
| \`order(s, ±0.1)\` | 相对加减 0.1 个币 |

**成交规则（默认）**
- 在 bar 收盘时决策，委托在**下一根 bar 开盘价**成交：买入价 = 开盘价 ×（1 + 半价差 + 滑点），卖出相反。
- 手续费按 taker 费率双向收取（默认 0.1%）；单根 bar 成交不超过该 bar 成交量的 10%。
- 调仓差额小于 max(10 USDT, 净值 × 0.5%) 的委托不成交——避免目标仓位法每根 bar 都微调。
- 现货只做多：仓位 0~1，可用现金不足时按可负担数量成交。
- 最后一根 bar 上的委托没有"下一根"可以成交，会在日志里提示。`,
  },
  {
    id: 'trade-mgmt',
    title: '持仓管理与订单类型',
    md: `**多周期**：\`history(s, 20, "close", unit="4h")\` 把本周期 K 线合成 4 小时线，只给已完整收盘的那些。

**止损止盈**（止损价只会收紧；每根 bar 收盘后更新，下一根起生效）
| 函数 | 含义 |
| --- | --- |
| \`set_stop_loss(0.05)\` / \`set_take_profit(0.1)\` | 固定比例止损 / 止盈 |
| \`set_trailing_stop(None, 0.05, activate=0.02)\` | 移动止损：浮盈到 2% 后启动，从最高点回撤 5% 平仓 |
| \`set_roi({0: 0.06, 60: 0.03, 240: 0})\` | 分段止盈：持有 0–60 分钟要 6%，1–4 小时要 3%，之后不亏就走 |

**持仓回调**（可选，不写就不调用）
| 回调 | 返回值 |
| --- | --- |
| \`custom_stoploss(ctx, trade)\` | 相对当前价的止损距离（如 0.03），或 None |
| \`custom_exit(ctx, trade)\` | 原因字符串或 True 表示平仓 |
| \`adjust_position(ctx, trade)\` | 加仓（正）/ 减仓（负）的金额 |
| \`confirm_entry(ctx, security, side, amount, price, tag)\` | False 否决这笔开仓 |

\`trade\` 只读：\`entry_price / profit_ratio / max_profit_ratio / bars_held / entry_tag / entries / stop_price / is_short\` 等。

**订单类型**：限价单可带 \`time_in_force="IOC"\`（下一根开盘吃不到就撤）、\`"post_only"\`（会吃单就撤，只做挂单方），
以及 \`on_expire="market"\`（到期改市价）/ \`"chase"\`（到期按开盘价重挂，最多 3 次）。

**标签**：下单的 \`reason="breakout"\` 就是入场标签 / 出场原因，回测详情的「交易分解」按它统计。

**组合保护**（只拦开仓，不拦止损和平仓）
\`\`\`python
PROTECTIONS = [
    {"method": "StoplossGuard", "lookback_bars": 24, "trade_limit": 3, "stop_bars": 12},  # 一天内止损 3 次，暂停 12 根
    {"method": "CooldownPeriod", "stop_bars": 2},                                         # 平仓后同一币冷却 2 根
    {"method": "MaxDrawdown", "lookback_bars": 48, "trade_limit": 4, "max_allowed_drawdown": 0.1, "stop_bars": 24},
    {"method": "LowProfitPairs", "lookback_bars": 72, "trade_limit": 2, "required_profit": 0.0, "stop_bars": 48},
]
\`\`\`
\`is_locked(s)\` 查当前是否锁定。回测详情的「保护与标的池」列出每次锁定。

**动态标的池**：每次只用当时已收盘的数据重选，不会选到还没上市的币。
\`\`\`python
PAIRLIST = {"refresh_bars": 24, "delist_exit": True, "filters": [
    {"method": "AgeFilter", "min_bars": 72},
    {"method": "VolumeFilter", "lookback_bars": 24, "top": 5},
]}
\`\`\`
\`get_universe()\` 取当前池子；下架公告一发布就移出池子（\`delist_exit\` 时平仓）。

**交易所模拟盘**：有止损价的持仓会在交易所挂保护止损单，平台停了也还在；交易所先触发时按交易所成交记账（出场原因 exchange_stop）。`,
  },
  {
    id: 'objects',
    title: '账户对象',
    md: `| 属性 | 说明 |
| --- | --- |
| \`ctx.securities\` | 标的池，如 \`["BTC/USDT", "ETH/USDT"]\` |
| \`ctx.current_dt\` | 当前时刻（毫秒时间戳，UTC） |
| \`ctx.params\` | 本次运行的参数（含覆盖值） |
| \`ctx.portfolio.total_value\` | 总权益 = 现金 + 持仓市值 |
| \`ctx.portfolio.available_cash\` | 可用现金 |
| \`ctx.portfolio.positions\` | \`{security: Position}\` |
| \`ctx.position(s)\` | 单个持仓（没有时返回数量为 0 的空持仓） |
| \`Position.amount / avg_cost / value / unrealized_pnl / pnl_ratio\` | 数量、成本均价（含费用）、市值、浮动盈亏、盈亏比例 |
| \`ctx.total_value\` / \`ctx.cash\` | 快捷方式 |
| \`g\` | 全局变量容器：\`g.entry_price = 0\`，在 initialize 里初始化 |`,
  },
  {
    id: 'params',
    title: '参数（PARAMS）',
    md: `在文件顶部用**字面量字典**声明可调参数：

\`\`\`python
PARAMS = {
    "fast": 12,        # 整数参数：覆盖时会自动取整
    "weight": 0.8,     # 浮点参数
}
\`\`\`

- 代码里用 \`PARAMS["fast"]\` 或 \`get_param("fast", 12)\` 读取；
- 编辑器「参数」页可以临时改值回测，不改代码；满意后「写回代码」生成新版本；
- 「参数优化」对 PARAMS 做网格搜索，并强制做样本内 / 样本外检验。`,
  },
  {
    id: 'settings',
    title: '设置与输出',
    md: `| 函数 | 说明 |
| --- | --- |
| \`set_benchmark("BTC/USDT")\` | 基准；不设置时取标的池第一个 |
| \`set_order_cost(taker_fee=0.001, slippage_bps=5)\` | 覆盖成本假设（实验假设，不代表交易所收费） |
| \`set_slippage(5)\` | 滑点（基点） |
| \`set_universe([...])\` | 修改标的池 |
| \`log.info(...)\` / \`log.warning(...)\` / \`log.error(...)\` | 日志，回测详情「日志输出」可见 |
| \`record(name=value)\` | 自定义曲线，回测详情「收益曲线 → 自定义」可见 |`,
  },
  {
    id: 'sandbox',
    title: '沙箱规则',
    md: `策略在受限环境中运行，以下写法会在保存或运行前被拒绝：

- 导入网络、系统、进程、文件相关模块（requests、socket、os、sys、subprocess、pathlib、time …）
- 使用 \`eval / exec / open / compile / __import__ / getattr / setattr / globals\`
- 访问以下划线开头的属性（如 \`__class__\`、\`ctx._runtime\`）
- numpy / pandas 的文件读写接口（\`np.load\`、\`pd.read_csv\`、\`to_csv\` …）
- async 函数

可以导入：\`math statistics random datetime collections itertools functools operator bisect heapq decimal fractions json re copy string typing dataclasses enum\`（numpy、pandas 若已安装也可用于计算）。

单次运行超过 60 秒（单根 bar 超过 10 秒）会被中止，日志会指出超时。`,
  },
  {
    id: 'modes',
    title: '回测、模拟与对照',
    md: `| 模式 | 数据 | 成交 | 用途 |
| --- | --- | --- | --- |
| 编译运行 | 同回测设置 | 同回测 | 快速检查代码，不保存记录 |
| 运行回测 | 真实历史行情（取不到时报「未接入数据」） | 下一根开盘 ± 成本 | 评估逻辑、成本与市场适应性 |
| 参数优化 | 同回测，切样本内 / 外 | 同回测 | 检验参数是否稳健 |
| 模拟交易 | 真实实时行情 | 同一套撮合代码 | 前向观察 7 天 / 30 天 |
| 实盘对照 | 导入真实成交 | — | 定位模拟与真实执行的差异 |

**模拟成交、官方测试成交与真实成交三类结果不合并展示。** 合成数据只能检验流程，不能作为策略结论。`,
  },
  {
    id: 'ai',
    title: 'AI 助手',
    md: `- **生成**：自然语言 → 完整策略代码。生成后自动做沙箱检查与合成数据试跑，失败会把问题回灌给模型自动修复（最多 2 轮）。
- **修改**：按指令改代码，返回完整新代码与差异，确认后才写入编辑器。
- **解释 / 修复 / 解读回测 / 审稿**：没有配置模型时也能用（规则分析），界面会标注"降级"。
- 模型在「AI 模型」里设置：支持 DeepSeek、通义、Kimi、智谱、OpenAI 兼容接口、Anthropic 与本地 Ollama / vLLM，可按用途分配模型。
- AI 只产出代码草案与分析，**不能下单**；生成的策略是草稿，必须人工批准后才能进入模拟交易。`,
  },
]

export default function ApiDocsPage(_: PageProps) {
  const [active, setActive] = useState('start')
  useEffect(() => {
    const els = SECTIONS.map((s) => document.getElementById(`doc-${s.id}`)).filter(Boolean) as HTMLElement[]
    const io = new IntersectionObserver((entries) => {
      const vis = entries.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)
      if (vis[0]) setActive(vis[0].target.id.replace('doc-', ''))
    }, { rootMargin: '-80px 0px -60% 0px' })
    els.forEach((e) => io.observe(e))
    return () => io.disconnect()
  }, [])
  return (
    <>
      <Heading eyebrow="API REFERENCE" title="策略 API 文档" description="平台策略 SDK 的完整参考。写法与聚宽保持一致，差异主要在：加密货币 7×24 交易、现货只做多、成交在下一根 bar 开盘、以及更严格的沙箱。" />
      <div className="docs-layout">
        <nav className="docs-nav">
          {SECTIONS.map((s) => (
            <a key={s.id} href={`#/docs`} className={active === s.id ? 'on' : ''} onClick={(e) => { e.preventDefault(); document.getElementById(`doc-${s.id}`)?.scrollIntoView({ behavior: 'smooth' }) }}>{s.title}</a>
          ))}
        </nav>
        <div className="docs-body">
          {SECTIONS.map((s) => (
            <section key={s.id} id={`doc-${s.id}`} className="panel">
              <div className="panel-heading"><h2>{s.title}</h2></div>
              <div className="panel-body"><Markdown text={s.md} /></div>
            </section>
          ))}
        </div>
      </div>
    </>
  )
}
