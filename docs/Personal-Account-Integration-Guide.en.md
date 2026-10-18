# QuantLab v0.1.0-preview.1 · Personal Account Integration Guide

> This step is optional. Strategy authoring, backtesting, optimization and paper trading work without an exchange account. Connect real records only when you want to compare simulated results with actual fills.

[中文](个人账户接入教程.md) | [Documentation index](README.en.md) | [Back to README](../README.md)

## Ways to connect

| Method | Best for | Required input | What the platform sees |
|---|---|---|---|
| CSV import (`imported_live`) | No API key, or unsupported exchange | Exchange fill/funding/balance CSV | Only the uploaded file |
| Read-only API key (`readonly_live`) | Periodic automatic synchronization | Key with read permission only | Balances and fills for selected symbols |
| Official testnet (`testnet`) | Practicing integration | Testnet key | Testnet data, always marked as test |

Both real-account methods are read-only. Read-only clients implement GET requests only. The separately gated micro-live channel is not part of this integration.

The platform never requests login passwords, SMS/email codes, authenticator codes, seed phrases, private keys or browser cookies.

## Method A: CSV import

1. Export fills from the exchange.
2. Open **Live comparison → Import real records**.
3. Select fills, cash flows or balances.
4. Map columns if automatic detection fails.
5. Import.

Imports are all-or-nothing: one invalid row rejects the entire file with row-level diagnostics.

| Record type | Required fields | Optional fields |
|---|---|---|
| Fills | time, symbol, side, quantity, price | fee, fee currency, order ID, client order ID, fill ID |
| Cash flows | time, currency, amount | type, reference |
| Balances | time, currency, available | frozen |

Decimal values are parsed exactly. Formula cells beginning with `=`, `+`, `-` or `@` are rejected to prevent CSV formula injection. Duplicate files are detected by content digest.

## Method B: Read-only API key

### Binance

1. Log in → avatar → **API Management** → create an API key.
2. Enable **reading only**. Do not enable spot/leveraged trading, futures, withdrawals, internal transfers or universal transfers.
3. Restrict access to trusted IPs and enter the deployment egress IP.
4. Copy the API key and secret.

### OKX

1. Log in → profile → **API** → create a V5 API key.
2. Enable **Read** only. Do not enable trading or withdrawals.
3. Restrict the key to the deployment egress IP and set a passphrase.
4. Copy the API key, secret and passphrase.

### Platform binding

Open **Live comparison → Personal accounts → Bind read-only account**:

1. choose exchange and environment;
2. confirm the key-creation checklist;
3. paste credentials and run detection.

Detection checks account identity, read-only permissions, IP restrictions and clock skew. Trading, withdrawal or transfer permissions cause rejection and the key is not saved. IP allowlisting is strongly recommended; missing IP restrictions produce a warning.

Credentials are encrypted with the dedicated master key and stored in separate ciphertext files. The database and logs retain only a connection ID, masked key and secret reference.

## Synchronization and comparison

- Enter symbols such as `BTC/USDT,ETH/USDT` and run **Sync balances and fills**.
- Synchronization runs on a background worker.
- Open **Live comparison**, choose a backtest or paper session and a real-record source, then generate a report.
- The report attributes timing, slippage, fees and implementation shortfall.
- Unsupported exchanges remain CSV-only.

## Troubleshooting

- Clock error: synchronize the server clock.
- Invalid key: verify permissions, passphrase and IP allowlist.
- Missing fills: confirm the symbol and synchronization window.
- Do not enable withdrawal or transfer permissions to “fix” an integration issue.
