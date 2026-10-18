# QuantLab v 0.1 preview · Operations and Recovery Manual

This manual is for deployment and on-call operators. The platform separates the API control plane from the worker execution plane. They communicate through the database; Redis is only a wake-up accelerator and can be lost without breaking correctness.

[中文](运维与恢复手册.md) | [Documentation index](README.en.md) | [Back to README](../README.md)

```
Browser ──HTTP──▶ API (qagent serve) ──runs table / wake-up──▶ PostgreSQL / Redis
                                                              ▲
                     Worker (qagent worker) ◀─lease claim─────┘
                       └─ isolated Python strategy subprocesses
```

## 1. Deployment

### 1.1 Docker Compose

```bash
mkdir -p secrets
python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())" > secrets/secret.key
chmod 600 secrets/secret.key
cp .env.example .env
docker compose up -d --build
```

Open `http://<host>:8000`. The first registered account becomes the deployment administrator. Further registration is normally invitation-only; `QAGENT_OPEN_REGISTRATION=1` enables open registration.

For network-isolated Python strategy execution:

```bash
docker compose -f docker-compose.yml -f deploy/docker-compose.isolation.yml up -d
```

The overlay grants only the capability needed to create network namespaces for the worker. Without it, the UI truthfully reports network isolation as unavailable.

### 1.1.1 Long-running cloud node

Recommended baseline: 2 vCPU / 4 GB RAM in a region close to the exchange. Use time synchronization, HTTPS reverse proxy, firewall rules and exchange IP allowlists. All compose services use `restart: unless-stopped`.

Operational behavior:

- workers renew leases every 20 seconds; expired runs become `RECOVERING`;
- recovery resumes from the latest checkpoint, normally within 1–2 minutes;
- unknown orders are queried by client order ID and never blindly resubmitted;
- a worker whose main loop is stalled for 180 seconds exits for Docker to restart;
- the API watchdog raises a node-loss alert after two minutes without a heartbeat;
- exchanges are rate-limited with per-exchange token buckets and `Retry-After` handling.

### 1.2 Local development

```bash
bash scripts/dev.sh
powershell -ExecutionPolicy Bypass -File scripts\dev.ps1
```

The default is SQLite under `~/.quant_agent`, local polling and JSON candle cache. Set `QAGENT_DATABASE_URL` and `QAGENT_REDIS_URL` for PostgreSQL/Redis.

## 2. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `QAGENT_DATABASE_URL` | SQLite | PostgreSQL connection string |
| `QAGENT_REDIS_URL` | empty | Redis wake-up channel |
| `QAGENT_HOME` | `~/.quant_agent` | Data directory |
| `QAGENT_SECRET_KEY_FILE` / `QAGENT_SECRET_KEY` | generated separately | Fernet master key |
| `QAGENT_WORKER` | `external` | Worker mode |
| `QAGENT_LEASE_MS` | `60000` | Run lease duration |
| `QAGENT_WORKER_MAX_RESIDENT` | `50` | Resident paper sessions per worker |
| `QAGENT_PAPER_TICK_SECONDS` | `30` | Paper-market polling interval |
| `QAGENT_SANDBOX` | `subprocess` | Python strategy runner |
| `QAGENT_SANDBOX_CPU_SECONDS` / `_MEMORY_MB` | `300` / `1536` | Per-strategy limits |
| `QAGENT_OPEN_REGISTRATION` | `0` | Open registration |
| `QAGENT_MICROLIVE` | `0` | Real spot micro-live switch |
| `QAGENT_REAL_PERP` | `0` | Real perpetual switch |

Workspace quotas are managed under **Settings → Workspace**.

## 3. Daily checks

| Item | Where | Healthy state |
|---|---|---|
| API liveness | `GET /api/healthz` | `status=ok` |
| Exchange reachability | `GET /api/health` | adapters reachable |
| Worker heartbeat | `/api/runs/workers` | heartbeat within one lease |
| Strategy isolation | startup log / platform settings | truthful process/rlimit/drop/netns status |
| Network availability | `/api/market/network` and banner | no persistent degradation |
| Stuck runs | runs page | no growing `QUEUED` or long `RECOVERING` |
| Audit | audit log | operations recorded without plaintext secrets |

## 4. Run state machine

```
QUEUED ──claim──▶ RUNNING ──▶ COMPLETED / FAILED / CANCELED
   ▲                │  ▲
   │           pause│  │resume
   │                ▼  │
   │              PAUSED
   └── RECOVERING ◀── lease expiry
```

- Claims use `SELECT ... FOR UPDATE SKIP LOCKED`.
- Every state write validates the lease owner and fencing token.
- Parameter optimization checkpoints after each completed combination.
- Paper sessions checkpoint after each bar: ledger, orders, strategy state and processed events.
- Deterministic event and signal IDs prevent duplicate fills during replay.

### 4.1 Common incidents

| Symptom | Likely cause | Action |
|---|---|---|
| Backtest remains `QUEUED` | No worker | Check worker container/heartbeat |
| Many `RECOVERING` runs | Repeated worker crashes or OOM | Inspect logs, adjust memory/slots |
| `TASK_FAILED` with resource limit | Strategy exceeded sandbox limits | Optimize strategy or raise limits |
| `QUOTA_EXHAUSTED` | Workspace quota | Admin adjusts quota |
| “Market data stale” | Exchange unavailable | Do not expect new orders until fresh data returns |
| Read-only connection expired | Exchange key disabled or IP mismatch | Re-detect or rebind |
| Redis down | — | Automatic database polling fallback |

Do not edit `runs.state` directly while workers are active. Use run commands so lease/fencing semantics remain intact.

## 4.2 Public deployment hardening

- keep registration invitation-only;
- terminate TLS at a reverse proxy and limit request bodies/timeouts;
- rate-limit authentication, uploads and AI endpoints at the proxy;
- collect health, worker, disk, memory, CPU, DB and 5xx metrics;
- back up database, object data and master key separately;
- test restore procedures regularly;
- use Linux for production isolation;
- run multi-worker, kill, clock-drift and network-partition drills before public exposure.

## 5. Backup and recovery

Back up three things and keep the master key separate:

| Item | Location | Method |
|---|---|---|
| Database | PostgreSQL `qagent` | `pg_dump -Fc` |
| Data volume | `appdata` | Ciphertext credentials, strategy files and cache |
| Master key | `secrets/secret.key` | Offline password manager/vault |

Restore database and data volume, restore the matching master key, then start the stack so migrations can run. Verify login, account re-detection, and that previously running paper sessions enter `RECOVERING` and resume.

### 5.1 Master-key rotation or exposure

1. Generate a new key.
2. Have users revoke and rebind read-only connections.
3. Re-enter model-provider keys.
4. Replace the master key and restart API/worker.
5. Delete old exchange keys in each exchange console; platform revocation does not revoke the exchange-side key.

## 6. Database migrations

- Migration files live under `src/quant_agent/platform/migrations/`.
- `qagent migrate` runs them; API and worker also check at startup.
- PostgreSQL advisory locks prevent duplicate execution.
- Published migrations are append-only.

## 7. Security baseline

- real-order paths are disabled by default;
- read-only clients expose GET operations only;
- keys with trading or withdrawal permissions are rejected before storage;
- credentials are encrypted in separate ciphertext files;
- CSV formula cells are rejected on import and neutralized on export;
- session cookies are `HttpOnly` and `SameSite=Lax`;
- writes require `X-Requested-With`;
- workspace membership is validated per request; reviewers are read-only.

## 8. Micro live (small real money)

Micro live requires all three layers:

1. deployment switch `QAGENT_MICROLIVE=1`;
2. workspace-admin risk acknowledgement;
3. no active kill state.

Keys must be Binance/OKX spot, trade-capable, withdrawal/transfer disabled and IP-restricted. Capital tiers default to 100 / 300 / 1,000 USDT with a hard cap of 10,000. Promotion to the next tier requires 14 days at the current tier and drawdown no greater than 10%.

Exchange stop-loss orders are mandatory. If the stop cannot be placed, new entries pause. Each start requires a six-digit confirmation code valid for five minutes. The emergency stop closes and stops all micro-live sessions. If the platform is unavailable, cancel orders, close positions and delete keys directly at the exchange, then place `microlive.KILL` under `QAGENT_HOME`.

## 9. Real perpetuals

Real perpetuals are a separate real-money channel:

- `QAGENT_REAL_PERP=1`, independent of micro live;
- dedicated secret directory and connection set;
- Binance/OKX linear USDT-M only after futures-account checks;
- trade-only keys, no withdrawals/transfers, IP allowlist;
- admin confirmation, per-order/total notional, leverage and daily-loss limits;
- margin and position mode must be declared;
- exchange conditional stop is mandatory;
- `real_perp.KILL` or the UI kill switch pauses new entries and attempts to close all exchange positions;
- Agents can inspect status but cannot enter confirmation codes or clear a kill state.
