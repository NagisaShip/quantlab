"""SDK 策略模板库（对标聚宽"新建策略 → 选择模板"）。

每个模板都是一份完整、可直接回测的策略文件：
- 顶部 `PARAMS` 声明可调参数（参数表单与参数优化都从这里读）；
- 只通过 `order_target_percent` 等平台 API 表达目标仓位；
- 只用已收盘 bar；包含止损或仓位上限中的至少一项风险约束。

模板只是起点，不代表任何收益承诺。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SdkTemplate:
    id: str
    name: str
    category: str
    description: str
    securities: list[str]
    timeframe: str
    code: str
    tags: list[str] = field(default_factory=list)
    difficulty: str = "入门"

    def to_dict(self, *, include_code: bool = True) -> dict[str, Any]:
        d = asdict(self)
        if not include_code:
            d.pop("code")
        return d


MA_CROSS = '''"""双均线趋势：快线上穿慢线持仓，下穿或触发止损清仓。"""

PARAMS = {
    "fast": 12,          # 快线周期（bar）
    "slow": 48,          # 慢线周期（bar）
    "weight": 0.8,       # 持仓时的目标仓位（占总权益）
    "stop_loss": 0.08,   # 相对持仓成本的止损比例
}


def initialize(ctx):
    set_benchmark(ctx.securities[0])
    run_daily(rebalance, time="close")


def rebalance(ctx):
    s = ctx.securities[0]
    closes = history(s, PARAMS["slow"], "close")
    if len(closes) < PARAMS["slow"]:
        return
    fast = sum(closes[-PARAMS["fast"]:]) / PARAMS["fast"]
    slow = sum(closes) / len(closes)
    record(fast_ma=fast, slow_ma=slow)

    pos = ctx.position(s)
    if pos.amount > 0 and closes[-1] < pos.avg_cost * (1 - PARAMS["stop_loss"]):
        log.info("触发止损，清仓")
        order_target_percent(s, 0, reason="stop_loss")
        return

    want = PARAMS["weight"] if fast > slow else 0.0
    held = pos.value / ctx.total_value if ctx.total_value else 0
    if abs(want - held) > 0.05:
        order_target_percent(s, want, reason="ma_cross")
'''

RSI_REVERSION = '''"""RSI 均值回归：超卖分批买入，回到中性区或超买卖出。"""

PARAMS = {
    "period": 14,
    "oversold": 30,
    "overbought": 70,
    "exit_level": 55,
    "weight": 0.6,
}


def rsi(values, period):
    gains, losses = 0.0, 0.0
    for a, b in zip(values[-period - 1:-1], values[-period:]):
        change = b - a
        if change > 0:
            gains += change
        else:
            losses -= change
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100 - 100 / (1 + rs)


def initialize(ctx):
    set_benchmark(ctx.securities[0])
    run_daily(step)


def step(ctx):
    s = ctx.securities[0]
    closes = history(s, PARAMS["period"] + 1, "close")
    if len(closes) < PARAMS["period"] + 1:
        return
    value = rsi(closes, PARAMS["period"])
    record(rsi=value)
    holding = ctx.position(s).amount > 0
    if not holding and value < PARAMS["oversold"]:
        order_target_percent(s, PARAMS["weight"], reason="rsi_oversold")
    elif holding and (value > PARAMS["overbought"] or value > PARAMS["exit_level"]):
        order_target_percent(s, 0, reason="rsi_exit")
'''

BOLLINGER = '''"""布林带突破：收盘站上上轨入场，跌破中轨离场。"""
import math

PARAMS = {
    "window": 20,
    "width": 2.0,
    "weight": 0.7,
}


def initialize(ctx):
    set_benchmark(ctx.securities[0])
    run_daily(step)


def step(ctx):
    s = ctx.securities[0]
    closes = history(s, PARAMS["window"], "close")
    if len(closes) < PARAMS["window"]:
        return
    mid = sum(closes) / len(closes)
    std = math.sqrt(sum((c - mid) ** 2 for c in closes) / len(closes))
    upper = mid + PARAMS["width"] * std
    price = closes[-1]
    record(upper=upper, middle=mid, price=price)
    holding = ctx.position(s).amount > 0
    if not holding and price > upper:
        order_target_percent(s, PARAMS["weight"], reason="break_upper")
    elif holding and price < mid:
        order_target_percent(s, 0, reason="below_middle")
'''

DONCHIAN = '''"""唐奇安通道突破（海龟简化版）：突破 N 日高点入场，跌破 M 日低点或 ATR 止损离场。"""

PARAMS = {
    "entry": 55,         # 入场通道（bar）
    "exit": 20,          # 离场通道（bar）
    "atr_period": 20,
    "atr_stop": 2.0,     # 入场价下方几倍 ATR 止损
    "weight": 0.6,
}


def atr(highs, lows, closes, period):
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    trs = trs[-period:]
    return sum(trs) / len(trs) if trs else 0.0


def initialize(ctx):
    set_benchmark(ctx.securities[0])
    g.entry_price = 0.0
    run_daily(step)


def step(ctx):
    s = ctx.securities[0]
    n = max(PARAMS["entry"], PARAMS["atr_period"]) + 1
    data = history(s, n, ["high", "low", "close"])
    if len(data["close"]) < n:
        return
    highs, lows, closes = data["high"], data["low"], data["close"]
    price = closes[-1]
    upper = max(highs[-PARAMS["entry"] - 1:-1])
    lower = min(lows[-PARAMS["exit"] - 1:-1])
    a = atr(highs, lows, closes, PARAMS["atr_period"])
    record(upper=upper, lower=lower)
    holding = ctx.position(s).amount > 0
    if not holding and price > upper:
        order_target_percent(s, PARAMS["weight"], reason="breakout")
        g.entry_price = price
    elif holding:
        stop = g.entry_price - PARAMS["atr_stop"] * a
        if price < lower or price < stop:
            order_target_percent(s, 0, reason="exit_channel" if price < lower else "atr_stop")
'''

GRID = '''"""有限仓位网格：以基准价为中心分档，下跌加仓、上涨减仓，仓位封顶。"""

PARAMS = {
    "grid_step": 0.02,   # 每档间距（相对基准价）
    "levels": 5,         # 单边档数
    "max_weight": 0.8,   # 仓位上限
    "reset_after": 0.15, # 价格偏离基准超过此比例时重设基准
}


def initialize(ctx):
    set_benchmark(ctx.securities[0])
    g.anchor = 0.0
    run_daily(step)


def step(ctx):
    s = ctx.securities[0]
    price = history(s, 1, "close")
    if not price:
        return
    price = price[-1]
    if g.anchor == 0 or abs(price / g.anchor - 1) > PARAMS["reset_after"]:
        g.anchor = price
        log.info("重设网格基准价 %.2f" % price)
    offset = (g.anchor - price) / g.anchor        # 正数 = 低于基准
    level = int(offset / PARAMS["grid_step"])
    level = max(-PARAMS["levels"], min(PARAMS["levels"], level))
    # 基准价处持一半上限仓位，每下跌一档加一档，每上涨一档减一档
    per_level = PARAMS["max_weight"] / (2 * PARAMS["levels"])
    target = PARAMS["max_weight"] / 2 + level * per_level
    target = max(0.0, min(PARAMS["max_weight"], target))
    record(grid_level=level, target=target)
    order_target_percent(s, target, reason="grid_level_%d" % level)
'''

DCA = '''"""定投再平衡：每周固定金额买入，仓位超过上限时减仓回到上限。

资金口径：这里只是把账户里已有的现金分批买入（内部交易，不产生收益）。
若要模拟“每周往账户里存钱再买入”，请在回测设置里打开“定期入金”（recurring_deposit）：
入金按单位净值申购份额，收益率按时间加权计算，存进来的钱不会被算成投资收益。
"""

PARAMS = {
    "weekday": 1,         # 1=周一
    "amount": 500,        # 每次定投金额（USDT）
    "max_weight": 0.9,
}


def initialize(ctx):
    set_benchmark(ctx.securities[0])
    run_weekly(invest, weekday=PARAMS["weekday"])
    run_daily(cap)


def invest(ctx):
    s = ctx.securities[0]
    if ctx.cash >= PARAMS["amount"]:
        order_value(s, PARAMS["amount"], reason="dca")


def cap(ctx):
    s = ctx.securities[0]
    weight = ctx.position(s).value / ctx.total_value if ctx.total_value else 0
    if weight > PARAMS["max_weight"] + 0.05:
        order_target_percent(s, PARAMS["max_weight"], reason="cap")
'''

MOMENTUM_ROTATION = '''"""多币种动量轮动：对标的池按"动量 × 趋势质量"打分，只持有得分最高的一个。

打分 = 区间涨幅 × 线性回归 R²（趋势越顺、得分越高）。新候选的得分需超过当前持仓一定倍数
才换仓，以减少来回切换的成本。全部得分为负时空仓。
"""
import math

PARAMS = {
    "lookback": 72,       # 打分窗口（bar）
    "switch_ratio": 1.2,  # 换仓门槛：新得分 > 当前得分 × 该倍数
    "weight": 0.95,
}


def score(closes):
    n = len(closes)
    logs = [math.log(c) for c in closes]
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(logs) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, logs))
    slope = sxy / sxx if sxx else 0.0
    ss_tot = sum((y - my) ** 2 for y in logs)
    ss_res = sum((y - (my + slope * (x - mx))) ** 2 for x, y in zip(xs, logs))
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    return (closes[-1] / closes[0] - 1) * max(r2, 0.0)


def initialize(ctx):
    set_benchmark(ctx.securities[0])
    g.holding = None
    run_daily(rotate)


def rotate(ctx):
    scores = {}
    for s in ctx.securities:
        closes = history(s, PARAMS["lookback"], "close")
        if len(closes) == PARAMS["lookback"]:
            scores[s] = score(closes)
    if not scores:
        return
    best = max(scores, key=scores.get)
    record(best_score=scores[best])
    if scores[best] <= 0:
        if g.holding:
            log.info("全部得分为负，空仓")
            order_target_percent(g.holding, 0, reason="all_negative")
            g.holding = None
        return
    if g.holding == best:
        return
    if g.holding and scores.get(g.holding, 0) > 0 and scores[best] < scores[g.holding] * PARAMS["switch_ratio"]:
        return
    if g.holding:
        order_target_percent(g.holding, 0, reason="rotate_out")
    order_target_percent(best, PARAMS["weight"], reason="rotate_in")
    log.info("轮动到 %s，得分 %.4f" % (best, scores[best]))
    g.holding = best
'''

INVERSE_VOL = '''"""逆波动率配置：按各标的近期波动率的倒数分配权重，每周再平衡（全天候思路的简化版）。"""
import math

PARAMS = {
    "window": 168,        # 波动率窗口（bar）
    "total_weight": 0.9,  # 总仓位
    "weekday": 1,
}


def initialize(ctx):
    set_benchmark(ctx.securities[0])
    run_weekly(rebalance, weekday=PARAMS["weekday"])


def rebalance(ctx):
    inv = {}
    for s in ctx.securities:
        closes = history(s, PARAMS["window"] + 1, "close")
        if len(closes) < PARAMS["window"] + 1:
            return
        rets = [b / a - 1 for a, b in zip(closes[:-1], closes[1:])]
        mean = sum(rets) / len(rets)
        vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))
        if vol > 0:
            inv[s] = 1 / vol
    total = sum(inv.values())
    for s, w in inv.items():
        order_target_percent(s, PARAMS["total_weight"] * w / total, reason="inverse_vol")
        record(**{"w_" + s.split("/")[0]: w / total})
'''

BUY_HOLD = '''"""买入持有：基线策略，用于衡量其他策略是否真的创造了超额。"""

PARAMS = {"weight": 1.0}


def initialize(ctx):
    set_benchmark(ctx.securities[0])
    run_daily(step)


def step(ctx):
    s = ctx.securities[0]
    if ctx.position(s).amount == 0:
        order_target_percent(s, PARAMS["weight"], reason="buy_hold")
'''

BLANK = '''"""新策略：在 initialize 里做设置，在定时函数里表达目标仓位。"""

PARAMS = {
    "weight": 0.5,
}


def initialize(ctx):
    set_benchmark(ctx.securities[0])
    # 每根 bar 收盘后运行一次 step；也可以用 run_weekly / run_monthly
    run_daily(step)


def step(ctx):
    s = ctx.securities[0]
    closes = history(s, 20, "close")      # 最近 20 根已收盘 bar 的收盘价
    if len(closes) < 20:
        return
    # 在这里写你的信号逻辑
    if closes[-1] > sum(closes) / len(closes):
        order_target_percent(s, PARAMS["weight"])
    else:
        order_target_percent(s, 0)
'''


FUNDING_ARB = '''"""资金费套利（期现对冲）：现货做多 + 永续做空，价格涨跌两边抵消，赚空头收到的资金费。

- 最近几次资金费率的均值高于入场阈值 → 建仓：现货多、永续空，名义价值相等；
- 均值跌破离场阈值（空头开始付费）→ 两边一起平；
- 两边数量偏离超过容忍带 → 只调永续这一腿，让它对齐现货；
- 永续默认 1 倍杠杆：强平价约在开仓价的 2 倍处；价格逼近强平价（剩不到 15%）时两腿一起平掉，下一轮按新价格重建，
  相当于把空头的浮亏落袋、补足保证金。
"""

PARAMS = {
    "weight": 0.45,        # 每条腿的名义价值占总权益的比例
    "leverage": 1,         # 永续杠杆（逐仓）
    "liq_buffer": 0.15,    # 价格离强平价不到 15% 时减仓重建
    "lookback": 3,         # 用最近几次资金费率的均值判断
    "entry_rate": 0.00005, # 均值高于它（单次 0.005%）才入场
    "exit_rate": 0.0,      # 均值低于它就离场
    "hedge_band": 0.05,    # 两腿数量偏离超过 5% 时重新对齐
}


def initialize(ctx):
    g.spot = next(s for s in ctx.securities if not s.endswith(":PERP"))
    g.perp = next(s for s in ctx.securities if s.endswith(":PERP"))
    g.rates = []
    g.last_ts = 0
    set_benchmark(g.spot)
    set_leverage(g.perp, PARAMS["leverage"])
    run_daily(rebalance)


def rebalance(ctx):
    fr = get_funding_rate(g.perp)
    if fr and fr["ts"] != g.last_ts:
        g.last_ts = fr["ts"]
        g.rates = (g.rates + [fr["rate"]])[-PARAMS["lookback"]:]
    if len(g.rates) < PARAMS["lookback"]:
        return
    avg = sum(g.rates) / len(g.rates)
    record(funding_avg=avg * 100)

    spot = ctx.position(g.spot).amount
    perp = ctx.position(g.perp).amount   # 空头为负
    holding = spot > 0 or perp < 0

    p = ctx.position(g.perp)
    if perp < 0 and p.liquidation_price and p.last_price > p.liquidation_price * (1 - PARAMS["liq_buffer"]):
        order_target(g.spot, 0, reason="逼近强平价：两腿一起平，下一轮重建")
        order_target(g.perp, 0, reason="逼近强平价：平空")
        return
    if not holding and avg > PARAMS["entry_rate"]:
        order_target_percent(g.spot, PARAMS["weight"], reason="入场：资金费为正")
        order_target_percent(g.perp, -PARAMS["weight"], reason="入场：做空永续对冲")
    elif holding and avg < PARAMS["exit_rate"]:
        order_target(g.spot, 0, reason="离场：资金费转负")
        order_target(g.perp, 0, reason="离场：平空")
    elif holding and spot > 0 and abs(spot + perp) / spot > PARAMS["hedge_band"]:
        order_target(g.perp, -spot, reason="对冲偏离，重新对齐")
'''


PERP_TREND = '''"""永续双向趋势：均线向上做多、向下做空，带杠杆和止损。"""

PARAMS = {
    "fast": 20,
    "slow": 60,
    "weight": 0.5,       # 名义价值占总权益（正 = 多，负 = 空）
    "leverage": 3,
    "stop_loss": 0.05,   # 相对开仓均价（空头为向上 5%）
}


def initialize(ctx):
    g.s = ctx.securities[0]
    set_leverage(g.s, PARAMS["leverage"])
    set_stop_loss(g.s, PARAMS["stop_loss"])
    run_daily(rebalance)


def rebalance(ctx):
    closes = history(g.s, PARAMS["slow"], "close")
    if len(closes) < PARAMS["slow"]:
        return
    fast = sum(closes[-PARAMS["fast"]:]) / PARAMS["fast"]
    slow = sum(closes) / len(closes)
    side = 1 if fast > slow else -1
    pos = ctx.position(g.s)
    if pos.amount * side <= 0:      # 空仓或方向相反：反手
        order_target_percent(g.s, side * PARAMS["weight"])
    record(liq_price=pos.liquidation_price)
'''


TEMPLATES: list[SdkTemplate] = [
    SdkTemplate(
        "blank", "空白策略", "入门", "最小可运行骨架：一个参数、一个定时函数。", ["BTC/USDT"], "1h", BLANK, ["入门"]
    ),
    SdkTemplate(
        "ma_cross",
        "双均线趋势",
        "趋势",
        "快慢均线交叉 + 成本止损 + 仓位上限。",
        ["BTC/USDT"],
        "1h",
        MA_CROSS,
        ["趋势", "择时"],
    ),
    SdkTemplate(
        "rsi_reversion",
        "RSI 均值回归",
        "均值回归",
        "RSI 超卖买入、回到中性区卖出。",
        ["BTC/USDT"],
        "1h",
        RSI_REVERSION,
        ["均值回归", "择时"],
    ),
    SdkTemplate(
        "bollinger",
        "布林带突破",
        "趋势",
        "收盘突破上轨入场，跌破中轨离场。",
        ["ETH/USDT"],
        "4h",
        BOLLINGER,
        ["趋势", "波动率"],
        "进阶",
    ),
    SdkTemplate(
        "donchian",
        "唐奇安通道（海龟）",
        "趋势",
        "N 日高点突破入场，M 日低点或 ATR 止损离场。",
        ["BTC/USDT"],
        "4h",
        DONCHIAN,
        ["趋势", "止损"],
        "进阶",
    ),
    SdkTemplate(
        "grid",
        "有限仓位网格",
        "震荡",
        "基准价上下分档，下跌加仓上涨减仓，仓位封顶。",
        ["BTC/USDT"],
        "1h",
        GRID,
        ["网格", "震荡"],
    ),
    SdkTemplate(
        "dca", "定投再平衡", "配置", "每周固定金额买入，超上限减仓。", ["BTC/USDT"], "1d", DCA, ["定投", "配置"]
    ),
    SdkTemplate(
        "momentum_rotation",
        "多币种动量轮动",
        "轮动",
        "按区间涨幅 × 趋势 R² 打分，持有得分最高的币种，设换仓门槛。",
        ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"],
        "4h",
        MOMENTUM_ROTATION,
        ["轮动", "多标的", "动量"],
        "进阶",
    ),
    SdkTemplate(
        "inverse_vol",
        "逆波动率配置",
        "配置",
        "按波动率倒数分配权重，每周再平衡。",
        ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
        "1h",
        INVERSE_VOL,
        ["配置", "多标的", "风险平价"],
        "进阶",
    ),
    SdkTemplate(
        "funding_arb",
        "资金费套利（期现对冲）",
        "永续合约",
        "现货做多 + 永续做空，赚空头收到的资金费；费率转负离场，两腿偏离自动对齐。",
        ["BTC/USDT", "BTC/USDT:PERP"],
        "1h",
        FUNDING_ARB,
        ["永续", "套利", "对冲", "资金费"],
        "进阶",
    ),
    SdkTemplate(
        "perp_trend",
        "永续双向趋势",
        "永续合约",
        "均线定方向，可做多也可做空，带杠杆与止损。",
        ["BTC/USDT:PERP"],
        "1h",
        PERP_TREND,
        ["永续", "做空", "杠杆"],
        "进阶",
    ),
    SdkTemplate(
        "buy_hold", "买入持有（基线）", "基线", "满仓持有，作为比较基线。", ["BTC/USDT"], "1h", BUY_HOLD, ["基线"]
    ),
]


def list_templates(*, include_code: bool = False) -> list[dict[str, Any]]:
    return [t.to_dict(include_code=include_code) for t in TEMPLATES]


def get_template(template_id: str) -> SdkTemplate | None:
    return next((t for t in TEMPLATES if t.id == template_id), None)
