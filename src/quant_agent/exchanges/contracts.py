"""Contract specifications and exchange rules used by the research engine.

The platform distinguishes **linear**, **inverse coin-margined** and
**delivery** contracts.  The tables are explicit research assumptions, not a
claim to reproduce every exchange risk-engine detail.  Every backtest manifest
stores the chosen exchange, contract type, settlement currency, contract face
value, MMR tier, expiry and liquidation fee/slippage assumption so a reader can
audit the result.

The ledger supports isolated/cross margin and one-way/hedge position modes.
Exchange-specific rules remain modular: ``contract_multiplier`` is a USD face
value for inverse contracts, while linear contracts use a base-coin multiplier.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any

Tier = tuple[float, float]

SUPPORTED_PERP_EXCHANGES = ("binance", "okx", "bybit")


@dataclass(frozen=True)
class ContractSpec:
    exchange: str
    symbol: str
    contract_type: str
    settlement: str
    max_leverage: float
    mmr_tiers: tuple[Tier, ...]
    liquidation_fee_bps: float
    liquidation_slippage_bps: float
    funding_interval_hours: int
    source: str = "platform assumptions"
    updated_at: str = "2026-10-18"
    margin_mode_scope: str = "isolated,cross"
    position_mode_scope: str = "one-way,hedge"
    contract_multiplier: float = 1.0
    expiry_ts: int | None = None
    base_asset: str = ""
    quote_asset: str = "USDT"

    def mmr_for_notional(self, notional: float) -> float:
        n = max(0.0, float(notional))
        for upper, mmr in self.mmr_tiers:
            if n <= upper:
                return mmr
        return self.mmr_tiers[-1][1]

    def tier_for_notional(self, notional: float) -> dict[str, Any]:
        n = max(0.0, float(notional))
        lower = 0.0
        for upper, mmr in self.mmr_tiers:
            if n <= upper:
                return {
                    "lower": lower,
                    "upper": upper,
                    "mmr": mmr,
                    "label": f"{lower:.0f}–{upper:.0f} {self.quote_asset}",
                }
            lower = upper
        upper, mmr = self.mmr_tiers[-1]
        return {"lower": upper, "upper": None, "mmr": mmr, "label": f">{upper:.0f} {self.quote_asset}"}

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["mmr_tiers"] = [{"upper": u, "mmr": m} for u, m in self.mmr_tiers]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ContractSpec:
        tiers = tuple((float(x["upper"]), float(x["mmr"])) for x in d.get("mmr_tiers") or [])
        return cls(
            exchange=d["exchange"],
            symbol=d["symbol"],
            contract_type=d.get("contract_type") or "linear",
            settlement=d.get("settlement") or "USDT",
            max_leverage=float(d.get("max_leverage") or 1),
            mmr_tiers=tiers,
            liquidation_fee_bps=float(d.get("liquidation_fee_bps") or 0),
            liquidation_slippage_bps=float(d.get("liquidation_slippage_bps") or 0),
            funding_interval_hours=int(d.get("funding_interval_hours") or 0),
            source=d.get("source") or "serialized assumption",
            updated_at=d.get("updated_at") or "2026-10-18",
            margin_mode_scope=d.get("margin_mode_scope") or "isolated,cross",
            position_mode_scope=d.get("position_mode_scope") or "one-way,hedge",
            contract_multiplier=float(d.get("contract_multiplier") or 1),
            expiry_ts=d.get("expiry_ts"),
            base_asset=d.get("base_asset") or "",
            quote_asset=d.get("quote_asset") or "USDT",
        )


_BINANCE_TIERS: dict[str, tuple[Tier, ...]] = {
    "BTC": ((50_000, 0.004), (600_000, 0.005), (3_000_000, 0.01), (12_000_000, 0.025), (float("inf"), 0.05)),
    "ETH": ((10_000, 0.005), (100_000, 0.006), (500_000, 0.01), (1_000_000, 0.025), (float("inf"), 0.05)),
}
_OKX_TIERS: dict[str, tuple[Tier, ...]] = {
    "BTC": ((50_000, 0.004), (600_000, 0.005), (3_000_000, 0.01), (12_000_000, 0.025), (float("inf"), 0.05)),
    "ETH": ((10_000, 0.005), (100_000, 0.006), (500_000, 0.01), (1_000_000, 0.025), (float("inf"), 0.05)),
}
_BYBIT_TIERS: dict[str, tuple[Tier, ...]] = {
    "BTC": ((50_000, 0.005), (250_000, 0.01), (1_000_000, 0.025), (float("inf"), 0.05)),
    "ETH": ((10_000, 0.005), (100_000, 0.01), (500_000, 0.025), (float("inf"), 0.05)),
}
_GENERIC_TIERS: tuple[Tier, ...] = ((50_000, 0.005), (250_000, 0.01), (1_000_000, 0.025), (float("inf"), 0.05))


@lru_cache(maxsize=512)
def contract_type_of(symbol: str) -> str:
    sym = (symbol or "").upper().strip()
    if sym.endswith(":INVERSE") or sym.endswith("-INVERSE") or sym.endswith("_INVERSE"):
        return "inverse"
    if sym.endswith(":DELIVERY") or sym.endswith("-DELIVERY") or sym.endswith("_DELIVERY"):
        return "delivery"
    if sym.endswith(":PERP") or sym.endswith("-PERP") or sym.endswith("_PERP") or sym.endswith(":SWAP"):
        return "linear"
    return "spot"


def is_derivative(symbol: str) -> bool:
    return contract_type_of(symbol) in ("linear", "inverse", "delivery")


def _inverse_face(exchange: str, base: str) -> float:
    if exchange == "bybit":
        return 1.0
    if base == "BTC":
        return 100.0
    if base == "ETH":
        return 10.0
    return 1.0


def spec_for(exchange: str, symbol: str, expiry_ts: int | None = None) -> ContractSpec:
    """Return an exchange-specific derivative spec or a conservative spot spec."""
    ex = (exchange or "binance").lower()
    sym = (symbol or "").upper()
    base = sym.split("/", 1)[0] if "/" in sym else sym
    kind = contract_type_of(sym)
    if kind in ("linear", "inverse", "delivery") and ex in SUPPORTED_PERP_EXCHANGES:
        tables = {"binance": _BINANCE_TIERS, "okx": _OKX_TIERS, "bybit": _BYBIT_TIERS}[ex]
        tiers = tables.get(base, _GENERIC_TIERS)
        known = base in tables
        inverse = kind == "inverse"
        delivery = kind == "delivery"
        if ex == "binance":
            max_lev, fee, slip = 125.0, 50.0, 10.0
            source = "Binance USDT-M / COIN-M tier approximation"
        elif ex == "okx":
            max_lev, fee, slip = 125.0, 50.0, 15.0
            source = "OKX swap / futures tier approximation"
        else:
            max_lev, fee, slip = 100.0, 50.0, 12.0
            source = "Bybit linear / inverse tier approximation"
        if delivery:
            max_lev = min(max_lev, 50.0)
        return ContractSpec(
            exchange=ex,
            symbol=sym,
            contract_type=kind,
            settlement=base if inverse else "USDT",
            max_leverage=max_lev,
            mmr_tiers=tiers,
            liquidation_fee_bps=fee,
            liquidation_slippage_bps=slip,
            funding_interval_hours=0 if delivery else 8,
            source=source if known else f"{source} (generic base asset)",
            updated_at="2026-10-18",
            margin_mode_scope="isolated,cross",
            position_mode_scope="one-way,hedge",
            contract_multiplier=_inverse_face(ex, base) if inverse or (delivery and inverse) else 1.0,
            expiry_ts=expiry_ts if delivery else None,
            base_asset=base,
            quote_asset="USD" if inverse else "USDT",
        )
    return ContractSpec(
        exchange=ex,
        symbol=sym,
        contract_type=kind,
        settlement="USDT",
        max_leverage=1.0,
        mmr_tiers=_GENERIC_TIERS,
        liquidation_fee_bps=50.0,
        liquidation_slippage_bps=10.0,
        funding_interval_hours=8,
        source="generic platform assumption",
        updated_at="2026-10-18",
        margin_mode_scope="isolated",
        position_mode_scope="one-way",
        contract_multiplier=1.0,
        expiry_ts=None,
        base_asset=base,
        quote_asset="USDT",
    )


__all__ = ["SUPPORTED_PERP_EXCHANGES", "ContractSpec", "Tier", "contract_type_of", "is_derivative", "spec_for"]
