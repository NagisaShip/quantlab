# QuantLab v0.1.0-preview.1 · Strategy Protocol and Data Conventions

This document defines what a strategy can observe, what it can emit, how orders are matched, how fills are recorded and how metrics are calculated. Backtests, paper trading and live comparison share the protocol; only the event source and matching venue differ.

[中文](策略协议与数据口径.md) | [Documentation index](README.en.md) | [Back to README](../README.md)

---

## 1. Numeric conventions

- Ledger, orders, fills and balances use `Decimal`.
- JSON money, quantity and price values are decimal strings such as `"1009.79"`.
- Floating point is limited to analytics and never flows back into the ledger.
- Quantities are rounded down by `qty_step`; prices are rounded by `price_tick`.
- Orders below the minimum notional are rejected (`ZERO_QUANTITY` or `BELOW_TOLERANCE`).

Golden example: cash 1000 → buy 1 at 100 with fee 0.1 → sell at 110 with fee 0.11 → cash `899.9 + 110 - 0.11 = 1009.79`.

## 2. Events

Each bar is an auditable event:

| Field | Meaning |
|---|---|
| `event_ts` | Bar close time in UTC milliseconds |
| `receive_ts` | Time the platform received the event |
| `sequence` | Monotonic sequence within a source |
| `source` | `binance`, `okx`, `csv:<id>` or `synthetic` |
| `instrument_id` | Example: `BTC/USDT` |
| `raw_ref` | Raw record reference, such as exchange open time or CSV row |

Rules:

1. Only completed events are visible. A strategy called after bar *t* can read data up to and including *t*.
2. A close-based signal fills at the next event open by default (`fill_at = next_open`).
3. `event_id` deduplication prevents duplicate fills during replay.
4. During stale-data periods the strategy is not called and no orders are emitted.
5. For duplicate cached events, the first received event wins.

## 3. Strategy formats

### 3.1 Rule DSL

The DSL is structured JSON interpreted by the platform. It does not call `eval` or `exec` and does not run user code.

- Indicators: `sma`, `ema`, Wilder `rsi`, Wilder `atr`, `highest`, `lowest`, `return`.
- Conditions: `gt`, `gte`, `lt`, `lte`, `cross_above`, `cross_below`, `consecutive`, `all`, `any`, `not`.
- Operands: indicator references, price references, constants, optional lags and multipliers.
- Position: weight or quantity, `max_weight`, `qty_step`, `insufficient_funds`.
- Risk: stop-loss, take-profit, daily trade limit and drawdown pause.
- If entry and exit match on the same bar, exit wins.
- Schema: `GET /api/dsl/schema`; validation: `POST /api/dsl/validate`.

### 3.2 Python SDK

Python strategies use a JoinQuant-style lifecycle:

- `initialize(ctx)` once per run;
- `handle_data(ctx)` and `on_bar(ctx, bar)` per closed bar;
- `on_trade` / `on_fill` / `on_order_filled` fill callbacks;
- `run_timer(interval_ms)` and `on_timer(ctx)` for timer callbacks;
- `snapshot(ctx)` / `restore(ctx, state)` for paper-session checkpoints;
- order helpers: `order`, `order_target`, `order_value`, `order_target_value`, `order_target_percent`;
- limit orders support `GTC`, `IOC`, `post_only` and expiry actions `cancel`, `market`, `chase`;
- trailing stops, ROI ladders, custom stop-loss, custom exit, position adjustment and entry veto callbacks;
- dynamic pair lists and delisting events are evaluated only with point-in-time data;
- `context.portfolio` and positions are read-only.

Python strategies run in a separate subprocess with a minimal environment. On Linux the runner can enforce CPU, memory and file-size limits, privilege dropping and optional network namespaces. Workspace admins can disable Python strategies and allow DSL only.

## 4. Signals and target positions

Every order becomes a deterministic signal with a target position:

```json
{
  "signal_id": "4f1c9a...",
  "run_id": "run-...",
  "strategy_version_id": "sv-...",
  "instrument_id": "BTC/USDT",
  "event_id": "BTC/USDT:1791850800000",
  "decision_ts": 1791850800000,
  "expires_at": 1791854400000,
  "reason": "fast MA crossed above slow MA",
  "target": {"instrument_id": "BTC/USDT", "kind": "weight", "value": "0.8", "unit": "fraction"},
  "order_type": "market",
  "limit_price": null,
  "status": "filled",
  "status_reason": "",
  "reason_code": ""
}
```

`target.kind` can be quantity, equity weight, quote value, quantity delta or value delta.

`signal_id` is derived from run, strategy version, instrument, event and signal ordinal. Replaying a checkpoint therefore identifies already-processed signals and does not duplicate fills.

### 4.1 Rejection codes

Common reasons include:

- `INSUFFICIENT_FUNDS`, `INSUFFICIENT_POSITION`;
- `RISK_LIMIT`, `RISK_TRADE_LIMIT`, `RISK_PRICE_DEVIATION`, `RISK_DRAWDOWN_PAUSE`;
- `SIGNAL_EXPIRED`, `ZERO_QUANTITY`, `BELOW_TOLERANCE`;
- `NOT_TRADABLE`, `DATA_INCOMPLETE`, `END_OF_DATA`;
- `ENTRY_VETOED`, `POST_ONLY_WOULD_TAKE`, `IOC_CANCELED`.

## 5. Risk layer

Orders pass through a risk layer that strategies cannot bypass:

- maximum position weight and gross exposure;
- maximum trades per day;
- maximum price deviation;
- drawdown pause;
- stop-loss and take-profit.

Weight-based targets can be clipped to limits; explicit quantity targets can be rejected. Every adjustment or rejection is recorded.

## 6. Matching model

| Item | Rule |
|---|---|
| Market order | Next event open ± slippage in bps |
| Limit order | Conservative bar model: strict penetration required; touch-only is ambiguous and does not fill |
| Stop / take-profit | If both are touched in the same bar, stop-loss wins |
| Fees | Configurable maker/taker fees by exchange and market |
| Liquidity | Default cap of 10% of bar volume |
| Gap handling | Fill at the actual next available open |

## 7. Ledger and perpetual contracts

- Spot and derivatives share the Decimal double-entry ledger.
- Ledger v2 supports linear USDT-M, coin-margined inverse contracts and delivery futures.
- Margin modes: isolated and cross. Position modes: one-way and hedge.
- Hedge mode keeps independent long and short legs and attributes fills with `position_side`.
- Cross margin calculates account-level equity and joint maintenance margin before liquidation.
- Funding is applied at settlement timestamps using mark price when available; missing mark price falls back to bar price and is flagged in the review card.
- MMR tiers, liquidation fee, liquidation slippage, contract multiplier, settlement currency and delivery expiry are recorded.
- Exchange-specific rules are isolated behind contract specifications and remain auditable research approximations.

## 8. Metrics

Every report carries an `assumptions` block covering sampling frequency, annualization factor, risk-free rate, benchmark, matching model, insufficient-funds behavior, limit-order model, stop priority, open-position treatment, warm-up and cost parameters.

| Metric | Definition |
|---|---|
| Return | Time-weighted return when cash flows exist; otherwise equity return |
| Annualized return | Geometric annualization with a short-sample warning |
| Volatility / Sharpe | Per-bar returns annualized by bars per year |
| Max drawdown | Peak, trough and recovery times; unresolved recovery is labeled |
| Worst day | Worst calendar-day return |
| Holding period | Completed round trips only |
| Total fees | Aggregated by currency and converted to quote value |

Open positions at the end of a backtest remain open and are excluded from win-rate and completed-trade metrics.

## 9. Research discipline

- hold out a final test segment before optimization;
- purge and embargo around split boundaries;
- walk-forward folds with out-of-sample reporting;
- write checkpoints after every parameter combination;
- record trial count and adjust for multiple testing.

## 10. Strategy versions

`DRAFT → VALIDATED → FROZEN → ARCHIVED`

Versions are immutable. Runs record `strategy_version_id`. Paper trading requires `FROZEN`; archived versions cannot start new runs.

## 11. Target-position envelopes

`GET /api/runs/{id}/envelopes` converts weight signals into a closed protocol for an external executor. Executors must reject expired, duplicate, out-of-order or malformed envelopes and compute their own order size from their own equity and positions.

`POST /api/envelopes/validate` uses server time when `now` is omitted. A real executor should use its own clock and apply its own risk limits.

## 12. Copy following

Follow modes use the follower’s own equity:

| Mode | Position source | Required parameter |
|---|---|---|
| `fixed_amount` | Fixed quote amount per signal | `fixed_amount` |
| `equity_ratio` | Follower equity × ratio × source target weight | `equity_ratio` |
| `risk_budget` | Follower equity × risk budget ÷ stop distance | `risk_budget`, `stop_loss_pct` |

Source closure behavior must be explicit: `keep`, `stop_new` or `exit`. There is no implicit default. Follower performance is calculated only from the follower’s fills and cash flows; source returns may appear as reference fields but cannot be presented as follower performance.

## 13. Run mode labels

| Mode | Source | Real money |
|---|---|---|
| `backtest` | Historical bars | No |
| `paper` | Live bars and simulated matching | No |
| `imported_live` | User-imported fills | Yes, user-provided |
| `readonly_live` | Read-only API synchronization | Yes |
| `testnet` | Exchange testnet | No |
| `agent` | Agent collaboration | No fills |
| `analysis` | Read-only analysis | No fills |

Modes are validated against an explicit allowlist. Without real fill data, live panels report “no live fills connected” and never substitute a simulated curve.
