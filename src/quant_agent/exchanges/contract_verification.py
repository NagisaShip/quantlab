"""交易所账单核验：把真实/模拟账户账单与平台合约假设对齐。

这里不猜测交易所后台规则。输入是交易所已经给出的账单行，输出是逐项检查：
资金费结算间隔、标记价、手续费、保证金模式、持仓方向、杠杆上限和强平费用。
无法从账单验证的项目明确写 ``unknown``，不能写成通过。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from .contracts import ContractSpec


def _dec(value: Any) -> Decimal | None:
    if value in (None, "", False):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


@dataclass
class BillRow:
    exchange: str
    ts: int
    symbol: str
    kind: str
    side: str = ""
    qty: Decimal | None = None
    price: Decimal | None = None
    notional: Decimal | None = None
    fee: Decimal | None = None
    fee_ccy: str = ""
    funding: Decimal | None = None
    funding_rate: Decimal | None = None
    mark_price: Decimal | None = None
    realized_pnl: Decimal | None = None
    leverage: Decimal | None = None
    margin_mode: str = ""
    position_mode: str = ""
    settle_ccy: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        for key, value in list(out.items()):
            if isinstance(value, Decimal):
                out[key] = format(value.normalize(), "f")
        return out


def normalize_bill(exchange: str, row: dict[str, Any]) -> BillRow:
    """把 OKX / Bybit / Binance 的常见账单字段归一成平台口径。"""
    ex = (exchange or "binance").lower()
    ts_raw = _first(row, "ts", "time", "transactionTime", "fundingRateTimestamp", "createTime", "uTime")
    ts = int(float(ts_raw or 0))
    symbol = str(_first(row, "symbol", "instId", "instrument") or "")
    if ex == "okx" and symbol.endswith("-SWAP"):
        symbol = symbol[: -len("-SWAP")].replace("-", "/") + ":PERP"
    if ex == "bybit" and symbol and "/" not in symbol and symbol.endswith("USDT"):
        symbol = symbol[:-4] + "/USDT:PERP"
    kind = str(_first(row, "kind", "type", "subType", "incomeType") or "trade").lower()
    qty = _dec(_first(row, "qty", "sz", "size", "execQty"))
    price = _dec(_first(row, "price", "avgPx", "tradePrice", "execPrice", "px"))
    notional = _dec(_first(row, "notional", "notionalValue", "tradeValue", "cumQuote"))
    if notional is None and qty is not None and price is not None:
        notional = qty * price
    fee = _dec(_first(row, "fee", "execFee", "commission", "feeAmount"))
    if ex == "okx" and fee is not None:
        fee = abs(fee)
    funding = _dec(_first(row, "funding", "fundingFee", "fundingAmount", "income"))
    if kind in ("funding", "funding_fee", "8", "fundingrate"):
        funding = funding if funding is not None else _dec(_first(row, "cashFlow", "change", "income"))
    return BillRow(
        exchange=ex,
        ts=ts,
        symbol=symbol,
        kind=kind,
        side=str(_first(row, "side", "direction") or "").lower(),
        qty=qty,
        price=price,
        notional=notional,
        fee=fee,
        fee_ccy=str(_first(row, "fee_ccy", "feeCurrency", "feeCcy", "asset") or ""),
        funding=funding,
        funding_rate=_dec(_first(row, "funding_rate", "fundingRate", "rate")),
        mark_price=_dec(_first(row, "mark_price", "markPrice", "indexPrice", "markPx")),
        realized_pnl=_dec(_first(row, "realized_pnl", "realizedPnl", "cashFlow", "change", "pnl")),
        leverage=_dec(_first(row, "leverage", "lever")),
        margin_mode=str(_first(row, "margin_mode", "mgnMode", "marginType", "tdMode") or "").lower(),
        position_mode=str(_first(row, "position_mode", "posSide", "positionSide", "positionIdx") or "").lower(),
        settle_ccy=str(_first(row, "settle_ccy", "settleCcy", "ccy", "asset") or "").upper(),
        raw=dict(row),
    )


def verify_bills(
    *, exchange: str, spec: ContractSpec, rows: list[dict[str, Any]], assumed_taker_fee_bps: float = 10.0
) -> dict[str, Any]:
    bills = [normalize_bill(exchange, r) for r in rows if isinstance(r, dict)]
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str, evidence: Any = None) -> None:
        checks.append({"name": name, "status": status, "detail": detail, "evidence": evidence})

    if not bills:
        return {
            "ok": False,
            "checks": [{"name": "账单", "status": "fail", "detail": "没有可核验的账单行", "evidence": None}],
            "summary": {"pass": 0, "warn": 0, "fail": 1, "unknown": 0},
            "spec": spec.to_dict(),
        }
    symbols = sorted({b.symbol for b in bills if b.symbol})
    add(
        "标的",
        "pass" if spec.symbol in symbols or not symbols else "warn",
        f"账单标的：{'、'.join(symbols) or '未提供'}",
    )
    funding = [b for b in bills if b.kind == "funding" or b.funding_rate is not None or b.funding is not None]
    if funding:
        times = sorted({b.ts for b in funding if b.ts})
        gaps = [round((b - a) / 3_600_000, 2) for a, b in zip(times, times[1:], strict=False)]
        good = (
            all(abs(g - spec.funding_interval_hours) <= max(1, spec.funding_interval_hours * 0.25) for g in gaps)
            if gaps
            else False
        )
        add(
            "资金费结算间隔",
            "pass" if good else "warn",
            f"观测间隔 {gaps[:8]} 小时，假设 {spec.funding_interval_hours} 小时"
            if gaps
            else "账单只有 1 个资金费结算点，无法验证间隔",
            {"gaps_hours": gaps[:20], "rows": len(funding)},
        )
        add(
            "资金费标记价",
            "pass" if any(b.mark_price and b.mark_price > 0 for b in funding) else "warn",
            "账单包含标记价"
            if any(b.mark_price and b.mark_price > 0 for b in funding)
            else "账单没有标记价字段，无法核对资金费名义价值",
            {"with_mark": sum(1 for b in funding if b.mark_price and b.mark_price > 0)},
        )
    else:
        add("资金费", "unknown", "账单中没有资金费行，无法验证结算与标记价")

    liquidation = [b for b in bills if "liquid" in b.kind or "force" in b.kind or "adl" in b.kind]
    if liquidation:
        add(
            "强平费用与标记价",
            "pass" if all(b.fee is not None and b.mark_price is not None for b in liquidation) else "warn",
            f"强平行 {len(liquidation)} 条；缺费用/标记价的字段不能当作已核验",
            {"rows": len(liquidation)},
        )
    else:
        add("强平样本", "unknown", "账单没有强平/ADL 行；MMR 与强平费用仍无法用真实样本验证")

    modes = sorted({b.margin_mode for b in bills if b.margin_mode})
    allowed_modes = {x.strip() for x in spec.margin_mode_scope.split(",") if x.strip()} | {"cash"}
    add(
        "保证金模式",
        "pass" if not modes or set(modes) <= allowed_modes else "warn",
        f"账单模式：{'、'.join(modes) or '未提供'}；平台研究假设：{spec.margin_mode_scope}",
        {"observed": modes, "allowed": sorted(allowed_modes)},
    )
    positions = sorted({b.position_mode for b in bills if b.position_mode})
    allowed_positions = {x.strip() for x in spec.position_mode_scope.split(",") if x.strip()} | {"net", "net_mode", "0"}
    add(
        "持仓模式",
        "pass" if not positions or set(positions) <= allowed_positions else "warn",
        f"账单方向：{'、'.join(positions) or '未提供'}；平台研究假设：{spec.position_mode_scope}",
        {"observed": positions, "allowed": sorted(allowed_positions)},
    )
    levs = [b.leverage for b in bills if b.leverage is not None]
    add(
        "杠杆上限",
        "pass" if not levs or max(levs) <= Decimal(str(spec.max_leverage)) else "fail",
        f"账单最大杠杆 {max(levs) if levs else '未提供'}；规格上限 {spec.max_leverage:g}",
        {"max": str(max(levs)) if levs else None},
    )
    fee_rows = [b for b in bills if b.fee is not None and b.notional and b.notional > 0]
    if fee_rows:
        observed = [float(b.fee / b.notional * 10000) for b in fee_rows]
        add(
            "手续费率",
            "pass" if max(observed) <= assumed_taker_fee_bps * 2.5 else "warn",
            f"观测 {min(observed):.2f}–{max(observed):.2f} bps，平台成本假设 {assumed_taker_fee_bps:g} bps",
            {"min_bps": min(observed), "max_bps": max(observed)},
        )
    else:
        add("手续费率", "unknown", "账单没有可计算费率的手续费与名义价值")
    bad_settle = sorted(
        {
            b.settle_ccy
            for b in bills
            if b.settle_ccy and b.settle_ccy not in (spec.settlement, "USDT" if spec.settlement == "USDT" else "")
        }
    )
    add(
        "结算币种",
        "pass" if not bad_settle else "fail",
        f"账单结算币种与 {spec.settlement} 假设一致"
        if not bad_settle
        else "账单出现非预期结算币种：" + "、".join(bad_settle),
        {"observed": sorted({b.settle_ccy for b in bills if b.settle_ccy}), "expected": spec.settlement},
    )
    summary = {k: sum(1 for c in checks if c["status"] == k) for k in ("pass", "warn", "fail", "unknown")}
    return {
        "ok": summary["fail"] == 0,
        "checks": checks,
        "summary": summary,
        "spec": spec.to_dict(),
        "rows": len(bills),
        "symbols": symbols,
        "assumption_note": "账单核验只能验证账单里出现的字段；未出现的 MMR/强平场景仍保持 unknown。",
    }


__all__ = ["BillRow", "normalize_bill", "verify_bills"]
