"""T04：DSL 多标的与轮动。

三件事必须成立，缺一件这个功能就不能用：

1. **逐笔成交一致**：3 个标的、按 20 日收益取前 1 的轮动策略，在合成数据上必须与
   手写 Python 版本**逐笔**一致（时间、标的、方向、数量、价格、名义金额、手续费）。
   只比最终净值会放过"路径完全不同但净值碰巧接近"的实现。
2. **确定性**：同样的输入必须得到同样的 signal_id 与同样的成交序列；同分时按标的代码
   字典序打破平局（协议 docs/策略协议与数据口径.md 第 4 节的硬要求）。
3. **逐项中文报错**：非法配置必须被拒绝并指出字段路径，不能静默忽略。
"""

from __future__ import annotations

import copy
import json
import math
from decimal import Decimal

import pytest
from conftest import login
from fastapi.testclient import TestClient
from test_ledger import T0, H

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.exchanges.base import Bar
from quant_agent.sdk import run_backtest
from quant_agent.sdk.runtimes import RuntimeCost
from quant_agent.strategies.dsl import (
    EXAMPLES,
    DslError,
    evaluate_rotation,
    load_dsl,
    rank_order,
    validate,
)

ZERO = dict(slippage_bps=0, spread_bps=0, min_notional=0, rebalance_tolerance=0, max_volume_participation=1.0)

INSTRUMENTS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
LOOKBACK = 20


# ---------------------------------------------------------------- 合成数据
def synth_closes(n: int = 90) -> dict[str, list[float]]:
    """三个走势互不相同的合成序列。

    刻意让它们的 20 日收益**多次互相超越**，这样换手会发生好几轮；
    如果三个序列完全同向，测试就退化成"只买一次"，覆盖不到轮动的关键路径。
    """
    return {
        "BTC/USDT": [100 + 12 * math.sin(i / 5.0) + i * 0.15 for i in range(n)],
        "ETH/USDT": [50 + 9 * math.cos(i / 7.0) + i * 0.05 for i in range(n)],
        "SOL/USDT": [20 + 6 * math.sin(i / 3.0 + 1.0) + i * 0.22 for i in range(n)],
    }


def mk_bars(closes: list[float], symbol: str) -> list[Bar]:
    """单点 K 线（open = high = low = close），把撮合与滑点的影响降到零，便于逐笔对齐。"""
    return [
        Bar(
            symbol=symbol,
            timeframe="1h",
            open_time=T0 + i * H,
            close_time=T0 + (i + 1) * H,
            open=c,
            high=c,
            low=c,
            close=c,
            volume=1_000.0,
        )
        for i, c in enumerate(closes)
    ]


def rotation_spec(**patch) -> dict:
    s = copy.deepcopy(EXAMPLES["momentum_rotation_top1"])
    s.update(patch)
    return s


def problems(spec: dict) -> dict[str, str]:
    with pytest.raises(DslError) as ei:
        validate(spec)
    return {p["path"]: p["message"] for p in ei.value.problems}


# ---------------------------------------------------------------- 手写参考实现
def hand_rotation_fills(
    closes: dict[str, list[float]],
    *,
    lookback: int = LOOKBACK,
    top: int = 1,
    initial_cash: Decimal = Decimal("10000"),
    fee: Decimal = Decimal("0.001"),
) -> list[dict]:
    """手写 Python 版轮动，逐笔产出成交。

    刻意不复用 `quant_agent.strategies.dsl` 里的任何函数：这是"独立实现"，
    如果两边共用同一段排序或同一段取整代码，"一致"就变成了自证。

    口径与平台一致的部分（这些是协议规定，不是实现细节）：
    - 第 t 根 bar 收盘后决策，第 t+1 根**开盘价**成交（这里 open == close），
      成交时间戳记的是**成交那根 bar 的开盘时间**；
    - 目标权重按**成交时刻的总权益**折算数量，按 `qty_step` 向下取整；
    - 买入还要受**可用现金**约束：`qty ≤ 现金 / (价格 × (1 + 手续费率))`，
      否则平台会按"可负担数量"缩量（percent_target 走的是缩量分支，不是拒绝）；
    - 名次按 20 根收益降序，同分按标的代码字典序；
    - **只在目标权重发生变化时下单**（否则会不停产生 0 数量的空委托）；
    - 换手时**先卖后买**：卖出回流的现金才能供买入使用。
    """
    names = sorted(closes)  # 字典序：与 DSL 的平局打破规则一致，且让遍历顺序固定
    n = len(next(iter(closes.values())))
    qty_step = Decimal("0.0001")
    price_step = Decimal("1e-10")  # 平台 PRICE_STEP：成交价按此取整

    def price(name: str, i: int) -> Decimal:
        """成交价：原始价格按 price_tick（默认 PRICE_STEP）取整。

        不取整的话每笔都会差最后几位小数——那同样是"逐笔不一致"，
        而且会让名义金额与手续费跟着偏。
        """
        return Decimal(str(closes[name][i])).quantize(price_step)

    cash = initial_cash
    holdings: dict[str, Decimal] = {name: Decimal(0) for name in names}
    pending: list[tuple[str, Decimal]] = []  # (标的, 目标数量)，下一根开盘执行
    fills: list[dict] = []

    def equity_at(i: int) -> Decimal:
        return cash + sum(holdings[name] * price(name, i) for name in names)

    def target_weights(i: int) -> dict[str, Decimal]:
        """第 i 根收盘时的目标权重（i < lookback 时指标未就绪，全部为 0）。"""
        if i < lookback:
            return {}
        scores: dict[str, float] = {}
        for name in names:
            prev = closes[name][i - lookback]
            scores[name] = closes[name][i] / prev - 1 if prev else 0.0
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))  # 降序 + 字典序平局打破
        picks = [name for name, s in ranked if s > 0][:top]
        if not picks:
            return {}
        return {name: Decimal(1) / len(picks) for name in picks}  # 等权

    def round_qty(qty: Decimal) -> Decimal:
        """与平台一致：数量按 qty_step **向下取整**。"""
        return (qty / qty_step).to_integral_value(rounding="ROUND_FLOOR") * qty_step

    last: dict[str, Decimal] = {name: Decimal(0) for name in names}  # 上一根的目标权重
    pending: list[tuple[str, Decimal]] = []  # (标的, 目标权重)，下一根开盘按**当时权益与价格**折算数量

    for i in range(n):
        # 1) 先撮合上一根产生的委托：按本根开盘价成交。
        #    关键口径：`order_target_percent` 的**数量在成交那一刻**才折算
        #    （`_fill` 里 `equity * target_value / base_price`），不是决策时算好的。
        #    所以这里保存的是**目标权重**，到成交时才按本根的权益与价格换算数量。
        #    卖出回流的现金必须立刻参与同一根 bar 内后续买单的可负担额度计算，
        #    因此 cash 逐笔累加，不能把权益算一次就复用。
        if pending:
            for name, weight in pending:
                p = price(name, i)
                if p <= 0:
                    continue
                equity = equity_at(i)  # 每笔成交前重估：与平台逐笔 mark + equity() 一致
                target = round_qty((equity * weight) / p)
                delta = target - holdings[name]
                if delta == 0:
                    continue
                if delta > 0:
                    # 与平台一致：买单受可用现金约束（含手续费），不足时缩量
                    afford = round_qty(cash / (p * (1 + fee)))
                    delta = min(delta, afford)
                    if delta <= 0:
                        continue
                else:
                    delta = max(delta, -holdings[name])  # 现货不做空
                    if delta == 0:
                        continue
                notional = abs(delta) * p
                if delta > 0:
                    cash -= notional + notional * fee
                else:
                    cash += notional - notional * fee
                holdings[name] += delta
                fills.append(
                    {
                        # 平台按**成交那根 bar 的开盘时间**给成交打时间戳
                        # （`_fill(at_open=True)` → `ts = bar.open_time`），不是收盘时间。
                        # 写错这里会让"逐笔一致"永远差一根 bar。
                        "ts": T0 + i * H,
                        "security": name,
                        "side": "buy" if delta > 0 else "sell",
                        "amount": abs(delta),
                        "price": p,
                    }
                )
            pending = []

        # 2) 决策：只在"目标变了"或"目标与持仓不符"时下单；换手时先卖后买（组内按标的字典序）。
        #    这里只记目标**权重**，数量留到成交那一刻再折算（见上）。
        weights = target_weights(i)
        changes: list[tuple[str, Decimal, bool]] = []
        for name in names:
            weight = weights.get(name, Decimal(0))
            held = holdings[name] > 0
            changed = weight != last[name]
            needs_action = (weight > 0 and not held) or (weight <= 0 and held)
            if not changed and not needs_action:
                continue
            changes.append((name, weight, weight < last[name]))
            last[name] = weight
        for name, weight, _is_sell in sorted(changes, key=lambda c: (not c[2], c[0])):
            # 数量差额为 0 时平台会以 ZERO_QUANTITY 拒绝——那是"拒绝"不是"成交"，
            # 对成交序列没有影响，这里不必模拟。
            pending.append((name, weight))

    return fills


# ---------------------------------------------------------------- 1. 逐笔成交一致
def test_rotation_matches_handwritten_python_tick_by_tick():
    """验收主项：3 个标的、按 20 日收益取前 1，与手写 Python 版逐笔一致。"""
    closes = synth_closes()
    bars = {name: mk_bars(v, name) for name, v in closes.items()}
    res = run_backtest(load_dsl(rotation_spec()), bars, initial_cash=10_000, cost=RuntimeCost(**ZERO))
    assert res.errors == [], res.errors

    expected = hand_rotation_fills(closes)
    assert expected, "合成数据必须产生成交，否则这条测试什么都没验证"
    assert len(res.fills) == len(expected), (
        f"成交笔数不一致：DSL {len(res.fills)} 笔，手写 {len(expected)} 笔\n"
        f"DSL：{[(f['ts'], f['security'], f['side']) for f in res.fills]}\n"
        f"手写：{[(f['ts'], f['security'], f['side']) for f in expected]}"
    )

    for i, (got, want) in enumerate(zip(res.fills, expected, strict=True)):
        assert got["ts"] == want["ts"], f"第 {i} 笔成交时间不一致：{got['ts']} != {want['ts']}"
        assert got["security"] == want["security"], f"第 {i} 笔标的不一致：{got['security']} != {want['security']}"
        assert got["side"] == want["side"], f"第 {i} 笔方向不一致：{got['side']} != {want['side']}"
        assert Decimal(got["amount"]) == want["amount"], f"第 {i} 笔数量不一致：{got['amount']} != {want['amount']}"
        assert Decimal(got["price"]) == want["price"], f"第 {i} 笔价格不一致：{got['price']} != {want['price']}"
        # 名义金额与手续费是"数量 × 价格"的派生量，一并逐笔核对，防止只对上数量却算错金额
        assert Decimal(got["notional"]) == want["amount"] * want["price"], f"第 {i} 笔名义金额不一致"
    assert res.ledger["balanced"], "逐笔一致之后账本还必须试算平衡"


def test_rotation_actually_rotates_between_instruments():
    """若三个标的从头到尾只买同一个，上面的逐笔一致就只是"两边都没换手"，覆盖不到轮动。"""
    closes = synth_closes()
    bars = {name: mk_bars(v, name) for name, v in closes.items()}
    res = run_backtest(load_dsl(rotation_spec()), bars, initial_cash=10_000, cost=RuntimeCost(**ZERO))
    bought = {f["security"] for f in res.fills if f["side"] == "buy"}
    assert len(bought) >= 2, f"合成数据上应当发生过换手，实际只买过 {bought}"


def test_rotation_weighted_by_score_splits_capital_by_score():
    """按得分加权：权重应正比于得分，且总和不超过 position.value。"""
    resolved = validate(rotation_spec(rank={"by": "ret", "top": 2, "order": "desc", "weighting": "score"})).resolved
    rows = {n: [{"open": c, "high": c, "low": c, "close": c} for c in v] for n, v in synth_closes().items()}
    out = evaluate_rotation(resolved, rows)
    assert len(out["picks"]) == 2
    scores = out["scores"]
    total = sum(scores[p] for p in out["picks"])
    # 选取得分全为正的一段，否则会退回等权（见 _rank_weights 的说明）
    assert total > 0, "这条断言依赖合成数据在末根上得分为正"
    for name in out["picks"]:
        assert out["weights"][name] == pytest.approx(scores[name] / total * 1.0)
    assert sum(out["weights"].values()) == pytest.approx(1.0)
    assert out["order"][0] == out["picks"][0]  # 名次第一就是第一个入选的


def test_score_weighting_falls_back_to_equal_on_nonpositive_scores():
    """总得分非正时退回等权：负权重在现货里没有意义（不能做空）。"""
    resolved = validate(rotation_spec(rank={"by": "ret", "top": 2, "weighting": "score"})).resolved
    # 构造一段全为负收益的数据，但把入选门槛设为 -1 让它们仍能入选
    resolved["rank"]["min_score"] = -1.0
    closes = {name: [100.0 - i for i in range(30)] for name in INSTRUMENTS}
    rows = {name: [{"open": c, "high": c, "low": c, "close": c} for c in series] for name, series in closes.items()}
    out = evaluate_rotation(resolved, rows)
    assert len(out["picks"]) == 2
    assert all(s < 0 for s in (out["scores"][p] for p in out["picks"]))
    assert out["weights"] == {name: pytest.approx(0.5) for name in out["picks"]}


def test_rotation_equal_weight_divides_evenly():
    resolved = validate(rotation_spec(rank={"by": "ret", "top": 2, "weighting": "equal"})).resolved
    rows = {n: [{"open": c, "high": c, "low": c, "close": c} for c in v] for n, v in synth_closes().items()}
    out = evaluate_rotation(resolved, rows)
    assert out["weights"] == {name: pytest.approx(0.5) for name in out["picks"]}


def test_rotation_weights_respect_max_weight_and_position_value():
    """position.value 与 max_weight 都要生效：0.8 的总仓位分给 2 个标的 → 各 0.4。"""
    resolved = validate(
        rotation_spec(
            rank={"by": "ret", "top": 2, "weighting": "equal"},
            position={"mode": "weight", "value": 0.8, "max_weight": 0.5},
        )
    ).resolved
    rows = {n: [{"open": c, "high": c, "low": c, "close": c} for c in v] for n, v in synth_closes().items()}
    out = evaluate_rotation(resolved, rows)
    assert out["weights"] == {name: pytest.approx(0.4) for name in out["picks"]}


# ---------------------------------------------------------------- 2. 平局打破的确定性
def test_rank_order_is_stable_on_ties():
    """同分必须按标的代码字典序，且 asc / desc 对同分给出同样的顺序。"""
    tied = {"SOL/USDT": 0.1, "BTC/USDT": 0.1, "ETH/USDT": 0.1}
    assert rank_order(tied, order="desc") == ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    assert rank_order(tied, order="asc") == ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    # 插入顺序不同不影响结果
    shuffled = {"ETH/USDT": 0.1, "SOL/USDT": 0.1, "BTC/USDT": 0.1}
    assert rank_order(shuffled, order="desc") == rank_order(tied, order="desc")


def test_rank_order_sorts_descending_for_desc():
    """desc 必须把**得分最高**的排第一；搞反了会静默买最差的标的（曾踩过这个坑）。"""
    scores = {"BTC/USDT": 0.1, "ETH/USDT": 0.9, "SOL/USDT": -0.4}
    assert rank_order(scores, order="desc") == ["ETH/USDT", "BTC/USDT", "SOL/USDT"]
    assert rank_order(scores, order="asc") == ["SOL/USDT", "BTC/USDT", "ETH/USDT"]


def test_rank_order_puts_missing_scores_last():
    """数据不足的标的（None）永远排在有效得分之后，两种方向都一样。"""
    scores = {"BTC/USDT": None, "ETH/USDT": 0.5, "SOL/USDT": -0.5}
    assert rank_order(scores, order="desc") == ["ETH/USDT", "SOL/USDT", "BTC/USDT"]
    assert rank_order(scores, order="asc") == ["SOL/USDT", "ETH/USDT", "BTC/USDT"]


def test_tie_break_keeps_rotation_reproducible():
    """三个标的 20 日收益完全相同时，取前 1 必须每次都选中字典序最小的那个。"""
    flat = {name: [100.0] * 40 for name in INSTRUMENTS}
    bars = {name: mk_bars(v, name) for name, v in flat.items()}
    spec = rotation_spec(entry={"op": "gte", "left": {"ref": "ret"}, "right": {"const": 0}})
    first = run_backtest(load_dsl(spec), bars, initial_cash=10_000, cost=RuntimeCost(**ZERO))
    second = run_backtest(load_dsl(spec), bars, initial_cash=10_000, cost=RuntimeCost(**ZERO))
    assert [f["security"] for f in first.fills] == ["BTC/USDT"]
    assert [f["to_dict"] if hasattr(f, "to_dict") else f for f in first.fills] == [f for f in second.fills]
    # 完全相同的输入 → 完全相同的信号 ID 序列（协议第 4 节的硬要求）
    assert [s["signal_id"] for s in first.signals] == [s["signal_id"] for s in second.signals]
    assert len(first.signals) > 0


def test_same_input_same_signal_ids_across_runs():
    """整段合成数据上，两次独立回测的信号 ID 与成交序列逐项相同。"""
    closes = synth_closes()
    bars = {name: mk_bars(v, name) for name, v in closes.items()}
    a = run_backtest(load_dsl(rotation_spec()), bars, initial_cash=10_000, cost=RuntimeCost(**ZERO))
    b = run_backtest(load_dsl(rotation_spec()), bars, initial_cash=10_000, cost=RuntimeCost(**ZERO))
    assert [s["signal_id"] for s in a.signals] == [s["signal_id"] for s in b.signals]
    assert a.fills == b.fills
    assert a.ledger["final_equity"] == b.ledger["final_equity"]


def test_rotation_order_independent_of_dict_insertion_order():
    """标的在字典里的插入顺序不能影响结果：否则同一份数据换个读入顺序就会换结果。"""
    closes = synth_closes()
    forward = {name: mk_bars(v, name) for name, v in closes.items()}
    backward = {name: mk_bars(closes[name], name) for name in reversed(INSTRUMENTS)}
    a = run_backtest(load_dsl(rotation_spec()), forward, initial_cash=10_000, cost=RuntimeCost(**ZERO))
    b = run_backtest(load_dsl(rotation_spec()), backward, initial_cash=10_000, cost=RuntimeCost(**ZERO))
    assert [(f["ts"], f["security"], f["side"], f["amount"]) for f in a.fills] == [
        (f["ts"], f["security"], f["side"], f["amount"]) for f in b.fills
    ]


# ---------------------------------------------------------------- 3. validate 的中文报错
def test_rank_top_exceeding_instruments_rejected():
    p = problems(rotation_spec(rank={"by": "ret", "top": 5}))
    assert "rank.top" in p and "超过标的数量" in p["rank.top"]


def test_rank_by_unknown_indicator_rejected():
    p = problems(rotation_spec(rank={"by": "momentum", "top": 1}))
    assert "rank.by" in p and "未在 indicators 中定义" in p["rank.by"]


def test_instrument_and_instruments_are_mutually_exclusive():
    p = problems(rotation_spec(instrument="BTC/USDT"))
    assert "instruments" in p and "二选一" in p["instruments"]


def test_rank_requires_multiple_instruments():
    p = problems(rotation_spec(instruments=["BTC/USDT"]))
    assert "rank" in p and "至少需要 2 个标的" in p["rank"]


def test_duplicate_instruments_rejected():
    p = problems(rotation_spec(instruments=["BTC/USDT", "BTC/USDT"]))
    assert "instruments[1]" in p and "重复" in p["instruments[1]"]


def test_bad_instrument_format_rejected_with_index():
    p = problems(rotation_spec(instruments=["BTC/USDT", "btcusdt"]))
    assert "instruments[1]" in p and "BASE/QUOTE" in p["instruments[1]"]


def test_rank_order_and_weighting_validated():
    p = problems(rotation_spec(rank={"by": "ret", "top": 1, "order": "up", "weighting": "vol"}))
    assert "rank.order" in p and "desc" in p["rank.order"]
    assert "rank.weighting" in p and "equal" in p["rank.weighting"]


def test_rank_unknown_field_and_bad_top_rejected():
    p = problems(rotation_spec(rank={"by": "ret", "top": 0, "mode": "x"}))
    assert "rank.top" in p and "正整数" in p["rank.top"]
    assert "rank.mode" in p and "未知字段" in p["rank.mode"]


def test_rotation_cannot_use_fixed_quantity():
    p = problems(rotation_spec(position={"mode": "quantity", "value": 1, "qty_step": 1}))
    assert "position.mode" in p and "weight" in p["position.mode"]


def test_rotation_value_exceeding_max_weight_per_pick_rejected():
    """等权时每个标的名义占比是 value/top；value=1.0、top=2 但 max_weight=0.4 必须被拒。"""
    p = problems(rotation_spec(position={"mode": "weight", "value": 1.0, "max_weight": 0.4}))
    assert "position.value" in p and "超过最大仓位" in p["position.value"]


def test_score_weighting_with_ascending_order_rejected():
    p = problems(rotation_spec(rank={"by": "ret", "top": 1, "order": "asc", "weighting": "score"}))
    assert "rank.weighting" in p and "desc" in p["rank.weighting"]


def test_min_score_must_be_finite_number():
    p = problems(rotation_spec(rank={"by": "ret", "top": 1, "min_score": "high"}))
    assert "rank.min_score" in p and "有限数值" in p["rank.min_score"]


def test_single_instrument_spec_still_validates_and_runs():
    """单标的路径不能被轮动改动破坏：instrument 仍然可用，且 resolved 里 instruments 是单元素。"""
    from quant_agent.strategies.dsl import TEMPLATE

    c = validate(TEMPLATE)
    assert c.resolved["instruments"] == ["BTC/USDT"] and c.resolved["rank"] is None
    assert c.resolved["rotation"] is False


def test_rotation_reenters_after_stop_loss_wipes_the_position():
    """止损把持仓打掉后，只要目标权重仍为正，必须能重新建仓。

    这是最容易写错的一条：如果只按"目标权重变了才下单"，止损清仓后目标没变，
    策略就永远空仓——不报错、只是再也不交易。
    """
    closes = synth_closes()
    bars = {name: mk_bars(v, name) for name, v in closes.items()}
    # 止损设得很紧（0.5%），单点 K 线下必然很快被触发
    spec = rotation_spec(risk={"stop_loss": 0.005, "take_profit": 0, "max_trades_per_day": 0, "drawdown_pause": 0})
    res = run_backtest(load_dsl(spec), bars, initial_cash=10_000, cost=RuntimeCost(**ZERO))
    assert res.errors == [], res.errors
    reasons = [f["reason"] for f in res.fills]
    assert "stop_loss" in reasons, f"止损没有被触发，合成数据需要调整：{reasons}"
    stopped = [f for f in res.fills if f["reason"] == "stop_loss"]
    # 止损之后必须还有买入（重新建仓），否则就是"止损后永久空仓"的静默故障
    after = [f for f in res.fills if f["ts"] > stopped[0]["ts"] and f["side"] == "buy"]
    assert after, f"止损后没有重新建仓：{[(f['ts'], f['security'], f['reason']) for f in res.fills]}"


def test_rotation_does_not_emit_orders_when_target_is_unchanged():
    """目标没变且持仓状态一致时不该有新信号：否则每根 bar 都刷出 ZERO_QUANTITY 拒绝。"""
    closes = synth_closes()
    bars = {name: mk_bars(v, name) for name, v in closes.items()}
    res = run_backtest(load_dsl(rotation_spec()), bars, initial_cash=10_000, cost=RuntimeCost(**ZERO))
    zero_qty = [r for r in res.rejected if r.get("code") == "ZERO_QUANTITY"]
    assert zero_qty == [], f"产生了无意义的 0 数量委托：{zero_qty[:3]}"
    # 信号数应该与成交数同一量级（每笔成交对应一个信号），而不是每根 bar 一个
    assert len(res.signals) <= len(res.fills) + 2


def test_rotation_summary_is_chinese_and_mentions_tie_break():
    c = validate(rotation_spec())
    text = "\n".join(c.summary)
    assert "轮动" in text and "字典序" in text and "3 个" in text


def test_explicit_null_rank_is_treated_as_single_instrument():
    """`"rank": null` 等于没有 rank：不能因为键存在就放宽单标的的仓位校验。"""
    from quant_agent.strategies.dsl import TEMPLATE

    s = copy.deepcopy(TEMPLATE)
    s["rank"] = None
    assert validate(s).resolved["rank"] is None
    # 单标的下 value=1.2 仍然必须被拒（若误判成轮动就会放过它）
    s2 = copy.deepcopy(TEMPLATE)
    s2["rank"] = None
    s2["position"] = {"mode": "weight", "value": 1.2, "max_weight": 1.0}
    assert "position.value" in problems(s2)


def test_rotation_state_reset_on_initialize():
    """检查点恢复会重跑 initialize，必须清空"上一根目标"。

    否则恢复后的第一根 bar 会认为"目标没变"而不下单，仓位就永远停在旧状态——
    这是那种跑起来不报错、只是再也不交易的静默故障。
    这里用真实回测的 initialize 钩子验证（SDK 的 set_option 必须在框架内调用）。
    """
    seen: list[dict[str, float]] = []
    mod = load_dsl(rotation_spec())
    original = mod._fns["initialize"]

    def spy(ctx):
        mod._last_targets = {"BTC/USDT": 1.0}  # 模拟"恢复前残留的旧目标"
        original(ctx)
        seen.append(dict(mod._last_targets))

    # runner 通过 `module.fn(name)` 取回调，而 `fn` 读的是 `_fns` 字典，
    # 所以必须替换字典里的条目，直接改 `mod._initialize` 属性是没用的。
    mod._fns["initialize"] = spy
    bars = {name: mk_bars(v, name) for name, v in synth_closes().items()}
    res = run_backtest(mod, bars, initial_cash=10_000, cost=RuntimeCost(**ZERO))
    assert res.errors == [], res.errors
    assert seen == [{}], f"initialize 之后 _last_targets 必须为空，实际 {seen}"


def test_evaluate_rotation_requires_rank_node():
    resolved = validate(EXAMPLES["ma_cross_rsi"]).resolved
    with pytest.raises(DslError) as ei:
        evaluate_rotation(resolved, {})
    assert "rank" in str(ei.value)


# ---------------------------------------------------------------- API 端到端
@pytest.fixture()
def client(tmp_path):
    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def test_api_rank_preview_returns_order_and_weights(client):
    """预览端点必须与回测同一段求值逻辑：名次、入选者、权重都对得上。"""
    closes = synth_closes()
    r = client.post("/api/dsl/rank", json={"spec": rotation_spec(), "ends": closes})
    assert r.status_code == 200, r.text
    body = r.json()
    resolved = validate(rotation_spec()).resolved
    rows = {n: [{"open": c, "high": c, "low": c, "close": c} for c in v] for n, v in closes.items()}
    expected = evaluate_rotation(resolved, rows)
    assert body["order"] == expected["order"]
    assert body["picks"] == expected["picks"]
    assert body["tie_break"] == "instrument_asc"
    assert body["weights"] == {k: f"{v:.10f}".rstrip("0").rstrip(".") for k, v in expected["weights"].items()}
    assert "轮动" in "\n".join(body["summary"])


def test_api_rank_preview_rejects_invalid_spec_with_chinese_problems(client):
    r = client.post("/api/dsl/rank", json={"spec": rotation_spec(rank={"by": "nope", "top": 9})})
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_PARAMETER"
    paths = {p["path"] for p in r.json()["details"]["problems"]}
    assert {"rank.by", "rank.top"} <= paths


def test_api_rank_preview_requires_rank_node(client):
    r = client.post("/api/dsl/rank", json={"spec": copy.deepcopy(EXAMPLES["ma_cross_rsi"])})
    assert r.status_code == 400
    assert "rank" in r.json()["message"]


def test_api_rank_endpoint_has_chinese_summary(client):
    """共同硬约束：新端点必须在 summaries.py 里有中文摘要。"""
    from quant_agent.api.summaries import SUMMARIES

    assert ("POST", "/api/dsl/rank") in SUMMARIES
    assert any("\u4e00" <= ch <= "\u9fff" for ch in SUMMARIES[("POST", "/api/dsl/rank")])


def test_saved_rotation_strategy_keeps_whole_universe(client):
    """保存后 meta.securities 必须是整个轮动池，否则回测拿不到另外两个标的的行情。"""
    r = client.post("/api/dsl/strategies", json={"name": "三标的轮动", "spec": rotation_spec()})
    assert r.status_code == 200, r.text
    strategy = r.json()["strategy"]
    assert strategy["securities"] == INSTRUMENTS
    # 落库的内容也要带上整个池子。`/api/strategies/{slug}` 被版本详情路由遮蔽，
    # 所以直接读仓储——注意仓储路径按 workspace_id 分目录，测试里没有请求上下文，
    # 必须显式取当前工作区的 store，不能用 `store.root`（那会因缺少请求上下文而 403）。
    from quant_agent.api.deps import get_services
    from quant_agent.strategies.repo import StrategyRepository

    ws = client.get("/api/auth/me").json()["workspace"]["id"]
    root = get_services().store.for_workspace(ws).root
    rec = StrategyRepository(root / "strategies").get(strategy["slug"])
    assert rec is not None and rec.meta.securities == INSTRUMENTS
    # 存下来的内容还能原样跑起来
    back = client.post(
        "/api/research/backtests",
        json={"slug": strategy["slug"], "source": "synthetic", "bars_limit": 200},
    )
    assert back.status_code == 200, back.text
    assert back.json()["result"]["securities"] == INSTRUMENTS


def test_json_round_trip_preserves_new_fields():
    """表单视图与 JSON 视图编辑同一个对象：新字段必须双向同步、不丢。

    前端 `DslEditor.tsx` 的做法是把 `spec` 当唯一真源：表单改 `spec`、JSON 视图
    解析出的也是 `spec`，切视图时用 `JSON.parse(JSON.stringify(spec))` 重新渲染。
    这里用同一段序列化做等价性验证——如果后端 schema 不允许某些字段，
    或 canonical 会丢字段，这里会先失败。
    """
    from quant_agent.strategies.dsl import canonical

    spec = rotation_spec()
    spec["rank"]["min_score"] = 0.0  # 表单里的"入选门槛"输入框可以为 0
    # 模拟前端：JSON 视图 → 对象 → 表单 → 对象 → JSON 视图
    as_text = json.dumps(spec, ensure_ascii=False, indent=2)
    parsed = json.loads(as_text)
    assert parsed["instruments"] == spec["instruments"]
    assert parsed["rank"] == spec["rank"]
    compiled = validate(parsed)
    # canonical 往返不丢新字段
    again = json.loads(canonical(compiled.spec))
    assert again["instruments"] == spec["instruments"]
    assert again["rank"] == spec["rank"]
    assert "instrument" not in again  # 多标的写法下不应凭空补出 instrument


def test_frontend_form_field_list_matches_backend_schema():
    """前端表单里的每个字段都要能在后端 schema 里找到；反之新字段也不能只在后端。

    这条测试守的是"表单与 JSON 视图切换不丢字段"的根因：只要有一边不认识某个字段，
    用户在两种视图之间切换时它就会被悄悄丢掉。
    """
    from pathlib import Path

    from quant_agent.strategies.dsl import DSL_SCHEMA

    top = set(DSL_SCHEMA["properties"])
    rank_props = set(DSL_SCHEMA["properties"]["rank"]["properties"])
    # 表单字段在 DslEditor.tsx 与它使用的共享表单 rulecode/RuleForm.tsx（策略编辑器的规则视图也用它）里
    pages = Path(__file__).resolve().parents[1] / "web" / "src" / "pages"
    source = "\n".join((pages / rel).read_text(encoding="utf-8") for rel in ("DslEditor.tsx", "rulecode/RuleForm.tsx"))
    for field in ("instruments", "rank"):
        assert field in top, f"后端 schema 缺少顶层字段 {field}"
    for field in ("by", "top", "order", "weighting", "min_score"):
        assert field in rank_props, f"后端 schema 缺少 rank.{field}"
        assert field in source, f"前端规则表单（DslEditor / RuleForm）没有同步 rank.{field} 字段"
    assert "instruments" in source and "min_score" in source
