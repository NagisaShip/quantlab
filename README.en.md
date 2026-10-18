# QuantLab v 0.1 preview

<div align="center">

**An AI-driven cryptocurrency quantitative research and simulation platform**

Research goals → multi-agent collaboration → strategy backtests → robustness review → research conclusions

[中文](README.md) | [English](README.en.md)

[![Python](./docs/images/badges/python.svg)](https://www.python.org/)
[![TypeScript](./docs/images/badges/typescript.svg)](https://www.typescriptlang.org/)
[![Backend tests](./docs/images/badges/backend-tests.svg)](docs/Test-and-Verification-Report.en.md)
[![License](./docs/images/badges/license.svg)](LICENSE)

</div>

![QuantLab home](docs/images/latest/en/01-home.png)

![Research loop demo: define a goal → multi-agent collaboration → backtest → robustness review → conclusion](docs/images/latest/en/demo-research-flow-en.gif)

*Research loop: goal → multi-agent collaboration → strategy backtest → robustness review → conclusion*

> **See it work first:** describe a research goal in the AI Lab. Multiple agents divide the work, call platform tools to build and backtest strategies, audit the evidence, and return a reproducible conclusion.

> **Preview release:** interfaces, data conventions and capability boundaries may change before a stable release.

---

## Safety boundary

- **No real-order path is enabled by default.** Read-only accounts, paper trading and exchange demo accounts never touch real money.
- **Micro live** is a separately gated real-money channel limited to Binance/OKX spot and graduated strategies.
- **Real perpetuals** are another independent channel limited to Binance/OKX linear USDT-M, disabled by default and protected by a dedicated secret store, admin confirmation, account checks, exposure/leverage/daily-loss caps, exchange stops and a kill switch.
- All results are for research only and are **not investment advice**.

---

## What QuantLab is

QuantLab is a conversation-first research platform:

- describe a research goal in the AI Lab;
- the Agent uses 51 platform tools across market data, strategy generation, backtests, factors, models, portfolios, execution and reports;
- every conclusion is tied to code hashes, parameters, data sources, cost assumptions and a run manifest;
- strategies must pass validation before paper trading, exchange demo or real-money micro live.

![Research Flow](docs/images/latest/en/17-research-flow.png)

Research Flow consolidates **7 stages, 56 stage-level entries and 27 cross-stage capabilities**. It includes review gates, benchmarks, ledger v2, research packages, the mentor, role and prompt libraries, model services, collaboration, notifications, feedback, favorites and API/tool references.

---

## Core capabilities

### 1. AI Lab

- 7 research types: auto, strategy, factor, model, portfolio, execution, full pipeline/report;
- 21 slash commands, 51 tool commands and 7 home commands;
- 16 built-in roles with parallel review, conflict resolution, red-team challenges and research auditing;
- background jobs report back to the conversation.

![AI Lab](docs/images/latest/en/02-ai-lab.png)

### Research mentor

Seven lessons cover hypothesis, data point-in-time, signal causality, backtests and benchmarks, robustness and multiple testing, perpetual risk, and execution/launch.

![Research mentor](docs/images/latest/en/18-mentor.png)

### 2. Strategy development

- rule DSL v2 with forms and JSON sharing one schema;
- JoinQuant-style Python SDK with 52 injected names;
- Freqtrade import with short/leverage support on derivatives;
- immutable versions: `DRAFT → VALIDATED → FROZEN → ARCHIVED`.

![Strategies](docs/images/latest/en/03-strategies.png)

### 3. Backtests and validation

- event-driven backtests with a Decimal double-entry ledger;
- next-bar execution, costs, liquidity caps and no future data;
- grid/Optuna optimization, walk-forward, PBO/CSCV, cost stress and experiment ledger;
- Research Review Gate blocks high-severity promotions;
- strategy, buy-and-hold and simple-rule benchmark curves.

![Backtests](docs/images/latest/en/05-backtests.png)


### 4. Execution and run modes

- paper sessions with worker checkpoints and crash recovery;
- Binance/OKX/Bybit exchange demo accounts;
- micro live for small real spot trades;
- real perpetuals for Binance/OKX linear USDT-M;
- live comparison and execution attribution from CSV or read-only APIs;
- reproducible research packages with verification and replay.

![Validation](docs/images/latest/en/06-validation.png)

![Paper](docs/images/latest/en/07-paper.png)

![Micro live](docs/images/latest/en/08-microlive.png)

### 5. Data and operations

- Binance/OKX/Bybit public data, local cache, gap filling and quality checks;
- research data catalog and point-in-time conventions;
- workspace roles, groups, mentions, notifications and audit logs;
- TOTP, quotas, idempotency, CSRF and Linux isolation.

![Data](docs/images/latest/en/09-data.png)

![Settings](docs/images/latest/en/14-settings.png)

---

## Quick start

```bash
mkdir -p secrets
python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())" > secrets/secret.key
cp .env.example .env
docker compose up -d --build
```

Open `http://127.0.0.1:8000`. The first registered account becomes the deployment administrator.

Local development:

```bash
pip install -r requirements-dev.lock
pip install --no-deps -e .
cd web && npm ci && npm run build && cd ..
qagent migrate
qagent serve --with-worker
```

LLM providers can be configured in the Models page, through environment variables, or `agent.config.toml`.

---

## Verification status

| Check | Result |
|---|---|
| Ruff lint / formatting | Passed |
| Full backend suite | **1,203 passed / 4 skipped** |
| TypeScript + Vite production build | Passed |
| Research Flow coverage audit | 30/44 direct, 8 contextual, 6 non-research |
| Playwright E2E | **32 passed** |
| English UI crawl | 0 visible Chinese UI strings on checked pages |
| API snapshot | 601 operations / 511 paths |

See the [Test and Verification Report](docs/Test-and-Verification-Report.en.md).

---

## Documentation

| Document | Content |
|---|---|
| [Documentation index](docs/README.en.md) | Reading order and capability boundaries |
| [Platform introduction](docs/Platform-Introduction.en.md) | Screenshots, feature map and workflows |
| [Test and verification report](docs/Test-and-Verification-Report.en.md) | Checks, tests and release checklist |
| [Strategy protocol](docs/Strategy-Protocol-and-Data-Conventions.en.md) | Events, signals, matching, ledger and metrics |
| [Exchange demo and packages](docs/Exchange-Demo-Trading-and-Strategy-Packages.en.md) | `.qstrat`, `qrun` and demo trading |
| [Operations and recovery](docs/Operations-and-Recovery-Manual.en.md) | Deployment, recovery, backup and real-money controls |
| [Isolation matrix](docs/Isolation-Capability-Matrix.en.md) | Linux/Windows isolation status |
| [Account integration](docs/Personal-Account-Integration-Guide.en.md) | CSV and read-only API keys |
| [API reference](docs/API-Reference.en.md) | English guide, complete endpoint index and OpenAPI contract |

---

## Perpetual capability boundary

| Entry | Spot | Perpetuals |
|---|---|---|
| Native backtest / AI Lab | Binance / OKX / Bybit | Linear, inverse and delivery; isolated/cross and one-way/hedge |
| `.qstrat` | Supported | Linear, inverse and delivery packages with golden-answer verification |
| Freqtrade import | Long-only spot | `can_short` / `leverage()` on derivatives |
| Micro live | Binance / OKX spot | Not supported |
| Real perpetuals | — | Binance / OKX linear USDT-M with separate safety controls |

Exchange-specific liquidation rules remain auditable research approximations and must be verified against exchange account bills.

## Known limitations

1. Windows isolation is process-only; POSIX resource and network isolation require Linux.
2. Micro live is Binance/OKX spot only.
3. Read-only synchronization has exchange lookback limits.
4. Backtest matching is bar-based; order-book execution lives in a separate simulation module.
5. `page-research` remains about 624KB after minification.

---

## Contributing

**Primary contributors: GitHub users [jamesusa99](https://github.com/jamesusa99) and [NagisaShip](https://github.com/NagisaShip).**

Please report security issues privately as described in [SECURITY.md](SECURITY.md).

## Contact

Email: [luoyangyuan110@gmail.com](mailto:luoyangyuan110@gmail.com)

## License

[MIT](LICENSE)
