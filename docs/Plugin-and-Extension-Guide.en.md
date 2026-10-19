# QuantLab Plugin and Extension Guide

> Applies to `v0.1.0-preview.1`. This document only describes extension points that **exist in the code**.
> Mechanisms that are not implemented are listed plainly under
> [Not supported today](#6-not-supported-today) — do not build against them.
>
> [中文](插件与扩展指南.md) | [Documentation index](README.en.md) | [API reference](API-Reference.en.md) | [Back to README](../README.en.md)

## Contents

- [0. Extension points at a glance](#0-extension-points-at-a-glance)
- [1. HTTP API conventions (read this first)](#1-http-api-conventions-read-this-first)
- [2. Strategy plugins (SDK)](#2-strategy-plugins-sdk)
- [3. Data-source plugins](#3-data-source-plugins)
- [4. Content-block plugins (`ql:*`)](#4-content-block-plugins-ql)
- [5. Capability switches (`platform/features.py`)](#5-capability-switches-platformfeaturespy)
- [6. Not supported today](#6-not-supported-today)
- [7. Pre-commit checklist](#7-pre-commit-checklist)

---

## 0. Extension points at a glance

| Extension point | Carrier | Code change? | Main location |
| --- | --- | --- | --- |
| Strategy | One `.py` file (strategy SDK) | No | Strategy page, or `POST /api/studio/strategies` |
| Your own time series | CSV: `ts,value[,available_at]` | No | [API reference · Data sources](API-Reference.en.md), `POST /api/datahub/import` |
| Built-in data connector (derivatives, macro, on-chain…) | `CONNECTORS` spec + fetch function | Yes | `research/datahub.py` |
| Market data source / exchange | Adapter class | Yes | `exchanges/base.py`, `exchanges/__init__.py` |
| Frontend content block | `ql:<type>` structured JSON | Yes | `agents/blocks_protocol.py` + `web/src/components/blocks/protocol.ts` |
| Workspace capability switch | `FEATURES` registry | Yes | `platform/features.py` |
| Agent tool | Tool function + prompt | Yes | `api/routes_*.py` + `agents/` |

Rule of thumb: **prefer the UI when it can already do the job** (strategy files, CSV import, model provider
registration). Only new *external data sources, exchanges or render types* require code changes.

---

## 1. HTTP API conventions (read this first)

Machine-readable spec: [`docs/openapi.json`](openapi.json). Interactive docs: `/docs` on a running instance.
The Chinese endpoint tables are generated from the FastAPI OpenAPI schema by `scripts/gen_api_doc.py`
(`docs/API参考.md`); CI checks that they match, so **never hand-edit `API参考.md`** — change the routes and
re-run the generator.

### Authentication and headers

| Item | Rule |
| --- | --- |
| Session | Cookie `qa_session`, or `Authorization: Bearer <token>` (`api/middleware.py:123`) |
| Write requests | With cookie auth you must send `X-Requested-With` (cross-site form protection, `api/middleware.py:132`); Bearer does not require it |
| Workspace | The request context selects the workspace; data never leaks across workspaces |
| Read-only reviewer | A reviewer account can only read; writes return 403 (`api/middleware.py:161`) |

### Error contract

Every error has the same shape (`platform/errors.py:1`):

```json
{ "code": "INVALID_PARAMETER", "message": "human readable text", "trace_id": "…", "details": {} }
```

`code` is a stable enum (`ErrorCode`) — branch on it, never parse `message`. Common values:
`UNAUTHENTICATED`, `PERMISSION_DENIED`, `NOT_FOUND`, `INVALID_PARAMETER`, `QUOTA_EXHAUSTED`,
`DATA_INCOMPLETE`, `MARKET_DATA_STALE`, `UNSUPPORTED_CAPABILITY`.

### Pagination and deprecation

- List endpoints take either `limit` or a cursor (e.g. `GET /api/runs?limit=&cursor=` returns `has_more`
  and `next_cursor`).
- Replaced endpoints carry a `Deprecation: true` response header, e.g. `GET /api/strategies`
  (`api/routes_strategies.py:20`).

---

## 2. Strategy plugins (SDK)

One strategy is one `.py` file. The same file runs unchanged in **backtest, paper/simulated trading and
live comparison** modes.

### 2.1 What you must write

- `initialize(ctx)`: register schedules (`run_daily` / `run_weekly` / `run_monthly` / `run_timer`) and set
  benchmark and costs (`set_benchmark` / `set_order_cost` / `set_slippage`).
- At least one callback: `handle_data(ctx)`, `on_bar(ctx)`, `on_fill(ctx)`, `on_order_filled(ctx)`,
  `before_trading_start(ctx)`, `after_trading_end(ctx)`.
- Express **target positions** with `order_target_percent(symbol, w)` / `order_target_value` / `order_target`
  instead of computing lot sizes yourself; matching, fees, slippage, funding and liquidation are handled by
  the ledger.

### 2.2 Minimal example

```python
# my_strategy.py — paste the whole file into the Strategy page, or POST /api/studio/strategies
def initialize(ctx):
    set_benchmark("BTC/USDT")
    g.fast, g.slow = 12, 48
    run_daily(rebalance, time="close")

def rebalance(ctx):
    s = ctx.securities[0]
    fast = ctx.indicators.ema(s, g.fast)   # precomputed by the platform, O(1) per bar
    slow = ctx.indicators.ema(s, g.slow)
    order_target_percent(s, 0.8 if fast > slow else 0.0)
```

### 2.3 Parameters

Declare module-level `PARAMS`, or read values inside `initialize` with `get_param("k", default)`. The UI
builds the parameter form automatically. The complete available API is the `__all__` list in
`src/quant_agent/sdk/__init__.py` (about 80 names).

### 2.4 Sandbox and static checks

| Mechanism | Detail |
| --- | --- |
| Static check | `check_source()` (`sdk/runner.py`) rejects `FORBIDDEN_NAMES`: `place_order`, `cancel_order`, `withdraw`, `api_secret`, `private_key`, `requests`, `httpx`, `urllib`, `socket`, `subprocess`, `os.system` |
| Injection | SDK names are injected; no `import` needed (`INJECTED`, `sdk/runner.py`) |
| Isolation | Backtests run in a controlled runtime; paper/live runs happen in an isolated subprocess with no exchange keys (`platform/isolation.py`, `platform/sandbox_exec.py`) |

### 2.5 Run it locally

```python
from quant_agent.sdk import load_file, run_backtest

result = run_backtest(load_file("my_strategy.py"), {"BTC/USDT": bars})
print(result.metrics["total_return"])
```

### 2.6 Verification

1. Strategy → New, paste the code. Saving fails loudly if a forbidden name is used — that is expected.
2. Run a backtest and inspect data sources, cost assumptions and checkup items on the `backtest/{id}` page.
3. Ask the AI reviewer (or check manually) about trade count, out-of-sample and 2× cost behaviour before
   approving the strategy.

---

## 3. Data-source plugins

### 3.1 Market data (bars)

`MarketDataService.get_bars(symbol, timeframe, n, source=…)` (`data/service.py:356`) accepts three sources:

| `source` | Behaviour |
| --- | --- |
| `auto` (default) | Tries real exchange data first; when exchanges are unreachable it does **not** silently fall back to synthetic bars (`allow_synthetic` is off by default, `data/service.py:339`) |
| `exchange` | Real data only; otherwise reports "no data connected" |
| `synthetic` | Teaching data only (requires `QAGENT_ALLOW_SYNTHETIC=1`) and is labelled as such in the UI |

The browser direct-connect path (`web/src/marketDirect.ts`) fetches bars straight from the exchange so charts
work without server-side internet access, but it **does not** write the server cache; backtests still use
server data.

### 3.2 Adding a built-in connector (code change)

Three steps, all in `src/quant_agent/research/datahub.py`:

1. **Register the spec** in `CONNECTORS` (`datahub.py:75`) with
   `{"id", "name", "category", "needs_key", "fields", "scope", "note", "url"}`. Add each field to
   `FIELD_SCOPE` (`datahub.py:35`) to say whether it is per symbol or market-wide.
2. **Implement the fetcher**: return `list[(ts, available_at, value)]`. `available_at` is when the value
   became publicly visible; factor research aligns as-of that timestamp. Keep publication lags for macro data
   (see `FRED_LAG_DAYS`, `datahub.py:56`).
3. **Wire the dispatch**: add an `elif connector == "your-id":` branch in `DataHub.sync()` (from
   `datahub.py:392`) and call `run(field, symbol, source, fetcher)` per field, so one failing field does not
   break the rest.

Connectors that need a key read it from `secrets_fn()["datahub:<id>"]`; a deployment admin fills it on the
Market & Data page (`POST /api/datahub/keys`, admin only, `api/routes_datahub.py:215`).

Sync and auto-backfill:

```http
POST /api/datahub/sync      # {connector, symbols[], period, start, end, series, name, wait}
POST /api/datahub/auto      # {enabled, interval_hours} lets the worker keep datasets up to date
GET  /api/datahub/datasets  # existing datasets (field × symbol × source)
GET  /api/datahub/datasets/{key}/series?limit=400
```

### 3.3 No code: upload your own time series

```http
POST /api/datahub/import
{ "name": "my_signal", "symbol": "BTC/USDT", "lag_hours": 0,
  "text": "ts,value\n1700000000000,0.42\n1700003600000,0.51\n" }
```

Reference it in factors as `x_<name>` (`api/routes_datahub.py:201`, implementation `datahub.py:719`).

### 3.4 Adding an exchange adapter (code change)

1. Implement the required methods on `BaseAdapter` / `ExchangeAdapter` in `exchanges/base.py`
   (bars, instruments, balances, `OrderIntent`; raise `UnsupportedFeature` for anything unsupported).
2. Register it in `public_adapter(exchange)` in `exchanges/__init__.py:18`; the `ccxt:<exchange>` prefix is
   already forwarded to `CcxtAdapter`.
3. Put contract specs in `exchanges/contracts.py` and register the capability boundary in
   `platform/features.py` (see section 5).

### 3.5 Verification

```bash
curl -s -X POST localhost:8000/api/datahub/sync -H 'content-type: application/json' \
  -H 'X-Requested-With: web' -b cookie.txt \
  -d '{"connector":"binance_futures","symbols":["BTC/USDT"],"period":"1h","wait":true}'
curl -s localhost:8000/api/datahub/datasets -b cookie.txt
```

An empty `errors[]` means success. In the UI: Market & Data → Data sources.

---

## 4. Content-block plugins (`ql:*`)

Agent answers, posts and reports may embed structured blocks:

````text
```ql:chart
{"title": "Equity", "kind": "line", "x": [1,2,3], "series": [{"name": "Strategy", "data": [1,1.2,1.5]}]}
```
````

The protocol exists **twice** — change both sides:

| Side | File | What to change |
| --- | --- | --- |
| Backend | `src/quant_agent/agents/blocks_protocol.py` | Type list + field description for the model |
| Frontend | `web/src/components/blocks/protocol.ts` | `BlockType`, `BLOCK_TYPES`, `BLOCK_LABEL` |
| Renderer | `web/src/components/blocks/RichBlock.tsx` | Add a `case 'your-type'` in the dispatcher |
| Styles | `web/src/stylesBlocks.css` | Any new class names |

Non-negotiable rules:

- Every block may carry `source` (where the data came from) and `evidence` (`measured` / `inferred` /
  `external`). Separating measurement from inference is the core convention of this platform — do not skip it.
- Never throw on malformed JSON: `parseBlock()` returns `{error, raw}` and the UI shows the raw text and
  reports it back to the Agent.
- Report render failures through `useReportRenderError`
  (`web/src/components/blocks/renderErrors.ts`) so the Agent can fix its own output.
- Inline HTML/SVG must go through `sanitizeHtml()` (DOMPurify, `web/src/components/blocks/Html.tsx:50`).
  Never bypass it.

---

## 5. Capability switches (`platform/features.py`)

Any feature that touches the market / execution / research boundary must be registered in `FEATURES` with
`default`, `label`, `implementation` (`ready` / `research` / `partial` / `not_implemented` / `blocked`),
`group` and `note`. The registry is the single source of truth for the API, the UI, Agent tools and docs:

- A capability that is not implemented **must not** be switchable by a workspace admin — this avoids the
  "the UI lets me select it but the engine cannot run it" failure mode.
- Read the current matrix at `GET /api/capabilities` (`api/routes_capabilities.py:16`).

---

## 6. Not supported today

- **No dynamic plugin loader.** You cannot drop a package in and have tools, routes or UI components
  auto-register; new routes, tools and block types are code changes that ship with a release.
- **No outbound webhooks or third-party OAuth apps.** The platform exposes an HTTP API with session auth and
  never calls external URLs back.
- **Strategies cannot use the network.** `requests` / `httpx` / `socket` etc. are rejected by the static check
  as part of the isolation design.
- **No third-party component sandbox in the UI.** `ql:*` blocks and inline HTML render whitelisted tags only;
  scripts cannot be injected (DOMPurify plus `safeHref` / `safeImageSrc` afterwards).
- **Users cannot add data connectors from the UI.** The UI can only store keys for existing connectors or
  import CSV.

---

## 7. Pre-commit checklist

```bash
python -m ruff check src tests scripts && python -m ruff format --check src tests scripts
python -m pytest -q                      # backend
python scripts/gen_api_doc.py && git diff --exit-code -- docs/API参考.md docs/openapi.json
cd web && npm run build                  # type check + build
cd .. && npm run e2e:typecheck
npm run qa:mermaid                       # Mermaid CJK compatibility self-check
npm run i18n:check                       # leftover-Chinese scan for the English UI
```

For UI copy: the Chinese string is the single source of truth. Add English to
`web/src/i18n/en.json`; template strings with variables go into `EN_PATTERNS` in `web/src/i18n.en.ts`.
Find gaps with `node web/scripts/i18n-extract.cjs`.
