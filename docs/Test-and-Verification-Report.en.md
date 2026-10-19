# QuantLab v0.1.0-preview.1 · Test and Verification Report

> Verification date: 2026-10-18
>
> Target: current workspace (`v0.1.0-preview.1`)
> Environment: Windows 11, Python 3.14.7, Node.js 26.5.0, Microsoft Edge for local Playwright runs

[中文](测试与验证报告.md) | [Documentation index](README.en.md) | [Back to README](../README.md)

---

## 1. Summary

| Check | Result |
|---|---|
| `ruff check src tests scripts` | Passed |
| `ruff format --check src tests scripts` | Passed, 338 files formatted |
| `pytest -q` | **1,208 passed / 4 skipped** |
| `npm run build` in `web/` | Passed; circular-chunk warning removed, `page-research` is about 624KB |
| `npm run e2e:typecheck` | Passed |
| `npm run e2e` | **33 passed** (10.5 min, Microsoft Edge, video disabled) |
| English UI crawl | No visible Chinese UI strings on the checked authenticated pages or landing page |
| Research Flow coverage | 30/44 pages direct, 8 contextual detail pages, 6 explicitly non-research pages |
| API snapshot | 604 operations / 513 paths |

---

## 2. Backend

### 2.1 Static checks

```bash
python -m ruff check src tests scripts
python -m ruff format --check src tests scripts
```

The lint configuration intentionally ignores `E501` for long Chinese messages, URLs and protocol text. Formatting is still enforced by `ruff format`.

### 2.2 Full test suite

```bash
python -m pytest -q
```

Result:

```text
1208 passed, 4 skipped
```

The remaining warnings are upstream FastAPI/Starlette/httpx deprecation notices.

### 2.3 Targeted coverage

Important paths covered by focused tests include:

- ledger v2: isolated and cross margin, independent long/short legs, joint liquidation, coin-margined settlement, delivery expiry and contract multipliers;
- real perpetuals: separate deployment switch, admin confirmation, kill switch, spot rejection, leverage/notional caps and dedicated connection set;
- Research Review Gate and research-package export/verify/replay;
- paper-session checkpoints and recovery;
- micro-live key permission checks and credential erasure after later permission changes;
- account bills and exchange contract-rule verification.

---

## 3. Frontend

### 3.1 Build

```bash
cd web
npm run build
```

`tsc -b && vite build` passes. Research pages share `page-research`; automation pages share `page-automation`. Circular chunk warnings are gone. Some third-party and research chunks still exceed 500KB; this is a performance warning, not a build failure.

`npm run qa:research` audits every `PageKey`: pages must either appear in Research Flow or be explicitly classified as contextual detail pages or non-research pages.

### 3.2 Refactoring evidence

- `strategy_lab.py` is split into `lab_tools.py`, `lab_state.py`, `lab_plan.py` and `lab_autopilot.py`, with compatibility entry points retained in the main class.
- Frontend API code is split into `api/core.ts`, `api/research.ts` and `api/platform.ts`; the legacy `api.ts` / `api2.ts` / `api3.ts` shims have been removed and all call sites import the split modules directly.
- `web/src/format.ts` centralizes number, money, quantity, date, query-string and download helpers.
- Removed the unused duplicate rating component and legacy API shims; added Chinese summaries for the community dislike/report endpoints and regenerated the OpenAPI snapshot.
- The E2E API now starts with `--no-with-worker` so `serve` cannot race the explicit worker for paper-session leases. The paper-fill wait budget is 240 seconds to tolerate CI / low-spec scheduling delays.
- Research Flow data moved to `web/src/pages/research-flow/data.ts`, reducing the page component to layout and interaction logic.
- `tests/test_onboarding.py` + `tests/e2e/09-onboarding.spec.ts`: first-registration state machine, skip/resume/reset, user isolation, Bot welcome layer, hands/gestures, step progression and persistence after reload.

### 3.3 Workspace-gating fix

The HTTP client previously treated any `403 PERMISSION_DENIED` whose message mentioned “workspace” as an invalid workspace selection. That could silently switch users to another workspace when a feature switch was disabled. The client now clears the workspace only when the server returns `details.workspace_id`. The Research Flow E2E covers the disabled-mentor state.

---

## 4. English UI audit

```bash
node scripts/qa-i18n.cjs
QA_LOGGED_OUT=1 node scripts/qa-i18n.cjs
```

The script:

- switches to `qa.lang=en`;
- visits each configured page;
- scans text nodes plus `placeholder`, `title` and `aria-label`;
- skips code editors, AI Markdown output and `data-no-i18n` areas;
- writes `tests/e2e/.artifacts/i18n/missing.json`.

Final targeted audit for `flow`, `mentor`, `realperp`, `backtests` and `settings` reports **0 missing visible strings**. Dynamic business data, strategy names and runtime paths are shown as data rather than rewritten.

---

## 4.1 Screenshots

Chinese screenshots are written to `docs/images/latest/`. English screenshots are generated with `QA_LANG=en` and `QA_OUT=docs/images/latest/en`; the English README and Platform Introduction use that directory.

## 5. End-to-end tests

### 5.1 Spec coverage

```bash
npm run e2e:list
```

The suite covers:

1. registration, login and logout;
2. DSL creation and backtest through an external worker;
3. immutable and frozen versions;
4. paper start, pause, resume and simulated liquidation;
5. CSV import;
6. live-comparison reports;
7. WebSocket reconnect and event-sequence continuity;
8. Research Flow, mentor state and mobile layout.

### 5.2 Final run

```powershell
$env:QAGENT_E2E_BROWSER_CHANNEL='msedge'
$env:QAGENT_E2E_VIDEO='off'
npm run e2e
```

Result:

```text
33 passed (10.5m)
```

CI should run the same suite against both SQLite and PostgreSQL storage legs with standard Playwright Chromium.

---

## 6. API snapshot

```bash
python scripts/gen_api_doc.py
```

Generated artifacts:

- `docs/API参考.md` — Chinese operation reference;
- `docs/API-Reference.en.md` — English entry guide;
- `docs/openapi.json` — machine-readable schema.

CI regenerates the snapshot and runs `git diff --exit-code` so documentation cannot silently drift.

---

## 7. Release checklist

- [x] Ruff lint and formatting;
- [x] Full backend suite;
- [x] TypeScript check and production build;
- [x] E2E typecheck and collection;
- [x] Full Playwright suite;
- [x] English UI crawl;
- [x] Research Flow coverage audit;
- [x] API snapshot;
- [x] README image paths validated;
- [x] No commit or push performed by this documentation pass.

### Remaining non-blocking items

1. `page-research` remains about 624KB after minification.
2. Windows strategy isolation provides process isolation but not POSIX resource limits or network namespaces.
3. Public multi-tenant hosting still requires production registration controls, TLS, rate limiting, monitoring, backup drills and Linux isolation.
4. Exchange-specific inverse/delivery and margin rules remain research approximations until reconciled against exchange account bills.
