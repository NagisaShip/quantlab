# QuantLab v 0.1 preview · Exchange Demo Trading and Strategy Packages

[中文](交易所模拟盘与策略包.md) | [Documentation index](README.en.md) | [Back to README](../README.md)

This guide covers the execution-side workflow: `.qstrat` packages, the `qrun` runner, the trading terminal and exchange demo accounts. Simulation is the default. Micro live and real perpetuals are separate, disabled-by-default real-money channels and never reuse demo endpoints.

## 1. `.qstrat` packages

A `.qstrat` file is a zip package containing:

| File | Purpose |
|---|---|
| `manifest.toml` | Name, author, version, market, contract type, margin/position mode, symbols, timeframe, SDK version and dependencies |
| `strategy.py` or `strategy.json` | Python source or DSL strategy |
| `params.json` | Parameters |
| `SPEC.md` | Human-readable strategy specification |
| `data/*.csv` | Deterministic verification data |
| `golden.json` | Reference fills, ending equity and code fingerprint |
| `lineage.json` | Optional trial count, DSR and research-discipline metadata |

- Export from the strategy editor.
- Import runs format/security checks and replays the golden answer in an isolated process before allowing import.
- Imported strategies are unapproved and require human review before paper trading.
- A package whose golden answer fails can be backtested, but cannot enter exchange demo trading.
- Perpetual packages support `:PERP`, `:INVERSE` and `:DELIVERY`; manifests record `contract_type`, `margin_mode`, `position_mode` and `expiry_ts`.
- Freqtrade `can_short` and `leverage()` are accepted only on derivative symbols.

### Workspace capability switches

Workspace admins can enable or disable spot, linear/inverse/delivery perpetuals, cross margin, hedge mode, real spot, real perpetuals, `.qstrat` perpetual packages, Freqtrade short/leverage and the research mentor. Unsupported or blocked capabilities cannot be enabled.

## 2. `qrun` runner

```bash
pip install -e .
qrun inspect  my.qstrat
qrun backtest my.qstrat
qrun backtest my.qstrat --market
qrun paper    my.qstrat --minutes 60
qrun demo     my.qstrat --exchange okx
```

Demo credentials are read from `QRUN_API_KEY`, `QRUN_API_SECRET` and, for OKX, `QRUN_PASSPHRASE`. `qrun` executes strategy code in the local process and performs static checks only; import untrusted packages through the platform.

## 3. Trading terminal

The paper-session detail page is a trading terminal:

- zoomable/pannable candles, fills and live updating bars;
- WebSocket live data with REST gap recovery and polling fallback;
- holdings, orders, fills and logs;
- account equity, execution mode, risk limits, slippage and reconciliation results.

Set `QAGENT_STREAM=0` to disable WebSocket streaming.

## 4. Exchange demo environments

| Environment | Endpoint | Price follows production |
|---|---|---|
| Binance Demo Trading | `demo-api.binance.com` | Close to production |
| Binance Spot Testnet | `testnet.binance.vision` | No; integration testing only |
| OKX Demo | `www.okx.com` with `x-simulated-trading: 1` | Close to production |
| Bybit Demo | `api-demo.bybit.com` | Close to production |
| Bybit Testnet | `api-testnet.bybit.com` | No; integration testing only |

Connect from **Exchange Demo**, verify the balance and then select the connection when starting a paper session with exchange-demo execution.

Safety controls include:

- exchange keys encrypted with the platform master key and available only to the worker;
- per-order, per-minute and daily-loss limits;
- deterministic client order IDs and query-before-retry behavior;
- explicit spot/perpetual endpoint allowlists;
- reconciliation before and after recovery.

## 5. Validation and acceptance

After demo trading works, use the Validation page to inspect fidelity, run stress drills and require human approval before promotion. Before accepting an exchange integration, run at least one day with a small strategy and compare orders, fills, slippage and reconciliation against exchange records.
