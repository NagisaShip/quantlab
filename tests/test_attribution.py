"""T08 · 归因：部分成交与拆单。

验收标准（docs/VibeCoding任务包.md）：
1 个模拟订单对应 3 笔真实部分成交 → 报告显示 1 行、加权均价与滑点 bps 正确（手算黄金样例）。

黄金样例（下面 test_three_partial_fills_merge_into_one_row 逐位手算）：
  模拟：数量 1、价格 60000，client_order_id="o1"
  真实：0.4 @ 60060 + 0.35 @ 60090 + 0.25 @ 60120，同一 client_order_id
  真实加权均价 = (0.4×60060 + 0.35×60090 + 0.25×60120) / 1 = 60085.5
  滑点 = (60085.5 / 60000 − 1) × 10000 = 14.25 bps（买入，正数表示买得更贵）
"""

from __future__ import annotations

from decimal import Decimal

from quant_agent.live.attribution import attribute

T0 = 1_700_000_000_000


def _sim(**kw):
    base = {
        "fill_id": "sim-1",
        "signal_id": "sig-1",
        "security": "BTC/USDT",
        "instrument": "BTC/USDT",
        "side": "buy",
        "amount": "1",
        "qty": "1",
        "price": "60000",
        "fee_value": "0.05",
        "ts": T0,
        "client_order_id": "o1",
    }
    base.update(kw)
    return base


def _live(qty, price, fee="0.01", **kw):
    base = {
        "fill_id": f"x-live-{qty}",
        "instrument": "BTC/USDT",
        "side": "buy",
        "qty": qty,
        "price": price,
        "fee": fee,
        "fee_currency": "USDT",
        "ts": T0 + 1000,
        "client_order_id": "o1",
    }
    base.update(kw)
    return base


def test_three_partial_fills_merge_into_one_row():
    """1 个模拟订单 ↔ 3 笔真实部分成交：报告 1 行，加权均价与滑点按手算值。"""
    live = [
        _live("0.4", "60060", ts=T0 + 1000),
        _live("0.35", "60090", ts=T0 + 2000),
        _live("0.25", "60120", ts=T0 + 3000),
    ]
    rep = attribute([_sim()], live)
    body = rep.to_dict()

    # 1 行，而不是 3 行
    assert len(body["pairs"]) == 1
    row = body["pairs"][0]

    # 归并后的真实侧：数量求和、价格按数量加权
    assert row["live"]["qty"] == "1"
    assert row["live"]["price"] == "60085.5"
    assert row["live_fills"] == 3
    assert row["sim_fills"] == 1

    # 滑点 bps 手算值
    assert row["slippage_bps"] == 14.25

    # 数量一致、手续费合计 0.03 − 0.05 = −0.02
    assert row["qty_diff"] == "0"
    assert row["fee_diff"] == "-0.02"

    sm = body["summary"]
    assert sm["attributed"] == 1
    assert sm["orders"] == 1
    assert sm["merged_orders"] == 1
    # live_fills 统计的是成交笔数，不是行数
    assert sm["live_fills"] == 3
    assert sm["unattributed"] == 0
    assert sm["unmatched_sim"] == 0
    assert any("拆成多笔成交" in n for n in body["notes"])


def test_all_three_partial_fills_are_attributed_not_dropped():
    """回归：逐笔匹配时第 2、3 笔会因模拟侧已被占用而掉进 unattributed。"""
    live = [
        _live("0.4", "60060", ts=T0 + 1000),
        _live("0.35", "60090", ts=T0 + 2000),
        _live("0.25", "60120", ts=T0 + 3000),
    ]
    body = attribute([_sim()], live).to_dict()
    assert body["unattributed"] == []
    assert body["summary"]["attributed_result"]["realized_pnl"] is not None


def test_sell_side_slippage_sign_is_inverted():
    """卖出时正滑点表示卖得更便宜，符号要与买入相反。"""
    sim = _sim(side="sell", client_order_id="o2")
    live = [
        _live("0.5", "59900", side="sell", client_order_id="o2", ts=T0 + 1000),
        _live("0.5", "59920", side="sell", client_order_id="o2", ts=T0 + 2000),
    ]
    row = attribute([sim], live).to_dict()["pairs"][0]
    # 加权均价 59910 < 60000，卖出更便宜 → 滑点为正
    assert row["live"]["price"] == "59910"
    assert row["slippage_bps"] == 15.0


def test_fills_without_order_key_are_not_merged():
    """没有订单标识的成交不能被并成一行——错误合并会伪造出一笔不存在的大额订单。"""
    live = [
        _live("0.4", "60060", client_order_id=None, fill_id="x1", ts=T0 + 1000),
        _live("0.6", "60090", client_order_id=None, fill_id="x2", ts=T0 + 2000),
    ]
    body = attribute([_sim(client_order_id=None, fill_id="s1")], live).to_dict()
    # 两笔都无标识 → 各自成组；一对一匹配只吃下一笔，另一笔进入 unattributed
    assert len(body["pairs"]) + len(body["unattributed"]) == 2
    assert body["summary"]["merged_orders"] == 0


def test_same_order_id_across_different_fill_ids_merges():
    """用交易所订单号（order_id）而不是 client_order_id 时同样能归并。"""
    sim = _sim(client_order_id=None, order_id="ex-9", fill_id="s1")
    live = [
        _live("0.5", "60100", client_order_id=None, order_id="ex-9", ts=T0 + 1000),
        _live("0.5", "60200", client_order_id=None, order_id="ex-9", ts=T0 + 2000),
    ]
    body = attribute([sim], live).to_dict()
    assert len(body["pairs"]) == 1
    assert body["pairs"][0]["live"]["price"] == "60150"
    assert body["pairs"][0]["live_fills"] == 2


def test_merged_sim_fees_are_summed_not_truncated():
    """模拟侧一个订单被拆成多笔时，手续费要合计，不能停在第一笔上。"""
    sims = [
        _sim(fill_id="s1", client_order_id="o3", order_id="o3", amount="0.4", qty="0.4", fee_value="0.02"),
        _sim(fill_id="s2", client_order_id="o3", order_id="o3", amount="0.6", qty="0.6", fee_value="0.03"),
    ]
    live = [_live("1", "60000", client_order_id="o3", order_id="o3", fee="0.05")]
    row = attribute(sims, live).to_dict()["pairs"][0]
    assert row["sim"]["fee_value"] == "0.05"
    assert row["fee_diff"] == "0"


def test_merge_uses_decimal_not_float():
    """加权均价必须是十进制精确值：0.1+0.2 这类二进制陷阱不能出现。"""
    sim = _sim(client_order_id="o4")
    live = [
        _live("0.1", "60000", client_order_id="o4", ts=T0 + 1000),
        _live("0.2", "60000", client_order_id="o4", ts=T0 + 2000),
    ]
    row = attribute([sim], live).to_dict()["pairs"][0]
    assert row["live"]["qty"] == "0.3"
    assert Decimal(row["live"]["price"]) == Decimal("60000")
