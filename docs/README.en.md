# QuantLab v 0.1 preview · Documentation Index

> Preview release. Interfaces, data conventions and capability boundaries may change before a stable release. The code, tests and `src/quant_agent/platform/features.py` remain the final authority.

[中文](README.md) | [中文 README](../README.md) | [English README](../README.en.md)

## Recommended reading order

1. [Platform Introduction](Platform-Introduction.en.md) / [中文](平台介绍.md) — screenshots, capability map and workflows.
2. [Test and Verification Report](Test-and-Verification-Report.en.md) / [中文](测试与验证报告.md) — checks, tests, build, E2E and release checklist.
3. [Strategy Protocol and Data Conventions](Strategy-Protocol-and-Data-Conventions.en.md) / [中文](策略协议与数据口径.md) — events, signals, matching, ledger and metrics.
4. [Exchange Demo Trading and Strategy Packages](Exchange-Demo-Trading-and-Strategy-Packages.en.md) / [中文](交易所模拟盘与策略包.md) — `.qstrat`, `qrun`, terminal and demo trading.
5. [Operations and Recovery Manual](Operations-and-Recovery-Manual.en.md) / [中文](运维与恢复手册.md) — deployment, recovery, backup and real-money switches.
6. [Isolation Capability Matrix](Isolation-Capability-Matrix.en.md) / [中文](隔离能力矩阵.md) — Linux/Windows isolation status.
7. [Personal Account Integration Guide](Personal-Account-Integration-Guide.en.md) / [中文](个人账户接入教程.md) — CSV and read-only API keys.
8. [API Reference](API-Reference.en.md) / [中文](API参考.md) — English guide with a complete endpoint index, Chinese operation reference and [openapi.json](openapi.json).

## Current capability boundary

- Spot: Binance / OKX / Bybit public market data, backtests, paper trading and exchange demo trading.
- Perpetuals: linear USDT-M, coin-margined inverse and delivery; isolated/cross margin and one-way/hedge positions; funding, MMR tiers, liquidation fees and slippage are recorded in the ledger.
- `.qstrat`: spot and linear/inverse/delivery perpetual packages, with symbol suffix, contract type, margin/position mode and golden-answer verification on import.
- Freqtrade: `can_short` and `leverage()` are accepted only on derivative symbols; spot remains long-only.
- Real money: micro live is Binance/OKX spot only; real perpetuals are Binance/OKX linear USDT-M only. Both are disabled by default and use separate safety controls.
- Research approximation: exchange-specific MMR, mark price, delivery price and liquidation rules still require account-bill reconciliation and are not a production exchange risk engine.

## Documentation maintenance

- Chinese and English documents are maintained as pairs.
- Do not document capabilities that are not implemented or are blocked by feature switches.
- Keep capability switches, exchange scope, contract types and liquidation assumptions aligned with `platform/features.py` and `exchanges/contracts.py`.
- Update test counts, API operation counts and screenshots after each full verification run.
- Product roadmaps, one-off design drafts and temporary patch notes do not belong in the long-lived documentation set.

## Contact

Email: [luoyangyuan110@gmail.com](mailto:luoyangyuan110@gmail.com)
