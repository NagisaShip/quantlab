"""仿真引擎层导出。"""

from .arena import DEFAULT_TRACK, Arena, Batch, Entrant, Standing, track_digest
from .backtest import BacktestEngine, BacktestResult, CostModel, Trade, stress_scenarios
from .metrics import BARS_PER_YEAR, Metrics, compute_metrics, drawdown_series
from .paper import (
    GAP_THRESHOLD_MS,
    STALE_QUOTE_MS,
    LiveFill,
    MatchedPair,
    PaperSession,
    PaperTradingEngine,
    ReconciliationReport,
    parse_live_fills_csv,
    reconcile,
)
from .windows import (
    HOUR_MS,
    WARMUP_BARS,
    WINDOW_DAYS,
    WindowResult,
    WindowSet,
    compute_windows,
    window_status_text,
)

__all__ = [
    "BARS_PER_YEAR",
    "DEFAULT_TRACK",
    "GAP_THRESHOLD_MS",
    "HOUR_MS",
    "STALE_QUOTE_MS",
    "WARMUP_BARS",
    "WINDOW_DAYS",
    "Arena",
    "BacktestEngine",
    "BacktestResult",
    "Batch",
    "CostModel",
    "Entrant",
    "LiveFill",
    "MatchedPair",
    "Metrics",
    "PaperSession",
    "PaperTradingEngine",
    "ReconciliationReport",
    "Standing",
    "Trade",
    "WindowResult",
    "WindowSet",
    "compute_metrics",
    "compute_windows",
    "drawdown_series",
    "parse_live_fills_csv",
    "reconcile",
    "stress_scenarios",
    "track_digest",
    "window_status_text",
]
