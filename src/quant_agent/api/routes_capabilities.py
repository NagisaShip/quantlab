"""Platform capability matrix: one source of truth for market/execution limits."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..exchanges.contract_verification import verify_bills
from ..exchanges.contracts import SUPPORTED_PERP_EXCHANGES, spec_for
from ..platform.features import enabled
from .deps import get_services

router = APIRouter(prefix="/api/capabilities", tags=["能力矩阵"])


@router.get("", summary="平台能力矩阵：现货/永续/杠杆边界")
def capabilities() -> dict:
    from ..platform import context

    try:
        ws = get_services().auth.workspace(context.current_workspace())
        features = enabled(ws.get("settings"))
    except Exception:
        features = enabled(None)
    specs = {ex: spec_for(ex, "BTC/USDT:PERP").to_dict() for ex in SUPPORTED_PERP_EXCHANGES}
    return {
        "workspace_features": features,
        "native_backtest": {
            "spot": {"supported": True, "enabled": features["market.spot"], "exchanges": ["binance", "okx", "bybit"]},
            "perp": {
                "supported": True,
                "enabled": features["market.perp_linear"],
                "exchange": "binance|okx|bybit",
                "exchanges": list(SUPPORTED_PERP_EXCHANGES),
                "contract_types": ["linear", "inverse", "delivery"],
                "settlements": ["USDT", "base coin (inverse)"],
                "margin_modes": ["isolated", "cross"],
                "position_modes": ["one-way", "hedge"],
                "liquidation": "simplified, exchange-rule approximation",
                "cross_margin": True,
                "hedge_mode": True,
                "inverse": True,
                "delivery": True,
                "specs": specs,
            },
        },
        "qstrat": {
            "spot": {"supported": True},
            "perp": {
                "supported": True,
                "enabled": features["package.qstrat_perp"],
                "contract_type": "linear",
                "settlement": "USDT",
                "reason": "仅支持线性 USDT-M 永续；反向/交割包会被拒绝",
            },
        },
        "freqtrade": {
            "spot": {"supported": True},
            "short": {"supported": True, "enabled": features["import.freqtrade_short"]},
            "leverage": {"supported": True, "enabled": features["import.freqtrade_short"]},
            "reason": "做空/杠杆仅在永续标的上运行；现货标的会拒绝做空信号",
        },
        "microlive": {
            "spot": {"supported": True, "enabled": features["execution.real_spot"]},
            "perp": {
                "supported": False,
                "enabled": False,
                "reason": "真实资金永续未开放；微实盘只做 Binance/OKX 现货",
            },
        },
        "real_money": {
            "spot": {"supported": True, "enabled": features["execution.real_spot"]},
            "perp": {
                "supported": True,
                "enabled": features["execution.real_perp"],
                "exchanges": ["binance", "okx"],
                "contract_type": "linear",
                "margin_modes": ["isolated", "cross"],
                "position_modes": ["one-way", "hedge"],
                "reason": "独立真实永续通道；需服务器开关、管理员确认码、期货 Key 自检、止损与自动急停",
            },
        },
        "data": {
            "funding": {"binance": True, "okx": True, "bybit": True},
            "mark_price": {"binance": True, "okx": "partial", "bybit": "partial"},
            "catalog": features["data.catalog"],
            "note": "标记价缺失时回测会回退到 bar 价格并在审查卡标注",
        },
    }


__all__ = ["router"]


class ContractBillsIn(BaseModel):
    exchange: str
    symbol: str = "BTC/USDT:PERP"
    rows: list[dict] = Field(default_factory=list, max_length=5000)
    assumed_taker_fee_bps: float = Field(10.0, ge=0, le=1000)


@router.get("/contracts/{exchange}/{symbol:path}", summary="读取交易所合约规格与研究假设")
def contract_spec(exchange: str, symbol: str) -> dict:
    return {"spec": spec_for(exchange, symbol).to_dict()}


@router.post("/contracts/verify-bills", summary="用交易所账户账单核验 MMR / 资金费 / 强平假设")
def contract_verify_bills(payload: ContractBillsIn) -> dict:
    ex = payload.exchange.lower()
    if ex not in SUPPORTED_PERP_EXCHANGES:
        raise HTTPException(400, f"不支持的永续交易所：{payload.exchange}")
    spec = spec_for(ex, payload.symbol)
    return verify_bills(exchange=ex, spec=spec, rows=payload.rows, assumed_taker_fee_bps=payload.assumed_taker_fee_bps)
