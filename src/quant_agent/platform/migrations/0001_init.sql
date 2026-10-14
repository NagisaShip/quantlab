-- QuantLab v2.0 初始结构
-- DECIMAL_T：PostgreSQL 为 NUMERIC(38,18)，SQLite 为 TEXT（避免二进制浮点）
-- AUTOINC_PK：PostgreSQL 为 BIGSERIAL PRIMARY KEY，SQLite 为 INTEGER PRIMARY KEY AUTOINCREMENT
-- 时间统一为 UTC 毫秒整数；JSON 存 TEXT

-- ---------- 用户、会话、工作区 ----------
CREATE TABLE users (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at BIGINT NOT NULL,
  last_login_at BIGINT
);

CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  user_id TEXT NOT NULL REFERENCES users(id),
  created_at BIGINT NOT NULL,
  expires_at BIGINT NOT NULL,
  revoked_at BIGINT,
  ip TEXT,
  user_agent TEXT
);
CREATE INDEX idx_sessions_user ON sessions(user_id);

CREATE TABLE workspaces (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  owner_id TEXT NOT NULL REFERENCES users(id),
  status TEXT NOT NULL DEFAULT 'active',
  is_demo INTEGER NOT NULL DEFAULT 0,
  settings TEXT NOT NULL DEFAULT '{}',
  created_at BIGINT NOT NULL
);

CREATE TABLE memberships (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  user_id TEXT NOT NULL REFERENCES users(id),
  role TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at BIGINT NOT NULL,
  PRIMARY KEY (workspace_id, user_id)
);
CREATE INDEX idx_memberships_user ON memberships(user_id);

CREATE TABLE invites (
  id TEXT PRIMARY KEY,
  code_hash TEXT NOT NULL UNIQUE,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  role TEXT NOT NULL,
  email TEXT,
  created_by TEXT NOT NULL REFERENCES users(id),
  created_at BIGINT NOT NULL,
  expires_at BIGINT NOT NULL,
  used_by TEXT,
  used_at BIGINT,
  revoked_at BIGINT
);

-- ---------- 审计与幂等 ----------
CREATE TABLE audit_log (
  id AUTOINC_PK,
  ts BIGINT NOT NULL,
  workspace_id TEXT,
  user_id TEXT,
  action TEXT NOT NULL,
  object_type TEXT,
  object_id TEXT,
  trace_id TEXT,
  ip TEXT,
  details TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_audit_ws_ts ON audit_log(workspace_id, ts);

CREATE TABLE idempotency_keys (
  workspace_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  idem_key TEXT NOT NULL,
  method TEXT NOT NULL,
  path TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  status_code INTEGER,
  response TEXT,
  created_at BIGINT NOT NULL,
  PRIMARY KEY (workspace_id, user_id, idem_key)
);

-- ---------- 策略版本（冻结后不可改写） ----------
CREATE TABLE strategy_versions (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  strategy_slug TEXT NOT NULL,
  version_no INTEGER NOT NULL,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  code TEXT NOT NULL,
  code_hash TEXT NOT NULL,
  params TEXT NOT NULL DEFAULT '{}',
  securities TEXT NOT NULL DEFAULT '[]',
  timeframe TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL,
  validation TEXT NOT NULL DEFAULT '{}',
  created_by TEXT NOT NULL,
  created_at BIGINT NOT NULL,
  archived_at BIGINT,
  UNIQUE (workspace_id, strategy_slug, version_no),
  UNIQUE (workspace_id, strategy_slug, code_hash)
);

-- ---------- 运行（回测 / 实时模拟 / 导入实盘 / 只读实盘 / 测试网） ----------
CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  created_by TEXT NOT NULL,
  mode TEXT NOT NULL,
  kind TEXT NOT NULL,
  state TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  strategy_version_id TEXT,
  manifest TEXT NOT NULL DEFAULT '{}',
  lease_owner TEXT,
  lease_expires_at BIGINT,
  attempts INTEGER NOT NULL DEFAULT 0,
  checkpoint TEXT,
  last_seq BIGINT NOT NULL DEFAULT 0,
  progress_done INTEGER NOT NULL DEFAULT 0,
  progress_total INTEGER NOT NULL DEFAULT 0,
  summary TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  idempotency_key TEXT,
  created_at BIGINT NOT NULL,
  started_at BIGINT,
  ended_at BIGINT,
  updated_at BIGINT NOT NULL,
  UNIQUE (workspace_id, idempotency_key)
);
CREATE INDEX idx_runs_ws ON runs(workspace_id, created_at);
CREATE INDEX idx_runs_state ON runs(state, lease_expires_at);

CREATE TABLE run_events (
  id AUTOINC_PK,
  run_id TEXT NOT NULL REFERENCES runs(id),
  workspace_id TEXT NOT NULL,
  ts BIGINT NOT NULL,
  type TEXT NOT NULL,
  data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_run_events_run ON run_events(run_id, id);

CREATE TABLE run_commands (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  workspace_id TEXT NOT NULL,
  command TEXT NOT NULL,
  status TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  idempotency_key TEXT,
  created_by TEXT NOT NULL,
  created_at BIGINT NOT NULL,
  applied_at BIGINT,
  UNIQUE (run_id, idempotency_key)
);

-- ---------- 信号、订单意图、成交、账本、净值 ----------
CREATE TABLE signals (
  signal_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  workspace_id TEXT NOT NULL,
  strategy_version_id TEXT,
  instrument_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  decision_ts BIGINT NOT NULL,
  expires_at BIGINT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  target TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL
);
CREATE INDEX idx_signals_run ON signals(run_id, decision_ts);

CREATE TABLE fills (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  workspace_id TEXT NOT NULL,
  fill_key TEXT NOT NULL,
  signal_id TEXT,
  order_id TEXT,
  instrument_id TEXT NOT NULL,
  side TEXT NOT NULL,
  qty DECIMAL_T NOT NULL,
  price DECIMAL_T NOT NULL,
  fee DECIMAL_T NOT NULL,
  fee_currency TEXT NOT NULL,
  ts BIGINT NOT NULL,
  source TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}',
  UNIQUE (run_id, fill_key)
);
CREATE INDEX idx_fills_run ON fills(run_id, ts);

CREATE TABLE ledger_entries (
  id AUTOINC_PK,
  run_id TEXT NOT NULL REFERENCES runs(id),
  workspace_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  ts BIGINT NOT NULL,
  account TEXT NOT NULL,
  currency TEXT NOT NULL,
  amount DECIMAL_T NOT NULL,
  ref_type TEXT NOT NULL,
  ref_id TEXT NOT NULL,
  memo TEXT NOT NULL DEFAULT '',
  UNIQUE (run_id, seq)
);

CREATE TABLE nav_points (
  run_id TEXT NOT NULL REFERENCES runs(id),
  ts BIGINT NOT NULL,
  nav DECIMAL_T NOT NULL,
  units DECIMAL_T NOT NULL,
  unit_nav DECIMAL_T NOT NULL,
  cash DECIMAL_T NOT NULL,
  positions_value DECIMAL_T NOT NULL,
  stale INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, ts)
);

-- ---------- 个人账户、导入、对照 ----------
CREATE TABLE account_connections (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  created_by TEXT NOT NULL,
  exchange TEXT NOT NULL,
  entity TEXT NOT NULL,
  label TEXT NOT NULL,
  key_mask TEXT NOT NULL,
  secret_ref TEXT,
  scope TEXT NOT NULL,
  status TEXT NOT NULL,
  last_test TEXT NOT NULL DEFAULT '{}',
  last_sync_at BIGINT,
  created_at BIGINT NOT NULL,
  revoked_at BIGINT
);

CREATE TABLE imports (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  created_by TEXT NOT NULL,
  kind TEXT NOT NULL,
  filename TEXT NOT NULL,
  rows INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  mapping TEXT NOT NULL DEFAULT '{}',
  report TEXT NOT NULL DEFAULT '{}',
  run_id TEXT,
  created_at BIGINT NOT NULL
);

CREATE TABLE comparisons (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  created_by TEXT NOT NULL,
  sim_run_id TEXT NOT NULL,
  live_run_id TEXT NOT NULL,
  status TEXT NOT NULL,
  result TEXT NOT NULL DEFAULT '{}',
  created_at BIGINT NOT NULL
);

-- ---------- 网络可用性实测 ----------
CREATE TABLE network_probes (
  id AUTOINC_PK,
  ts BIGINT NOT NULL,
  provider TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  ok INTEGER NOT NULL,
  latency_ms INTEGER,
  stage TEXT NOT NULL,
  error TEXT
);
CREATE INDEX idx_probes_ts ON network_probes(provider, ts)
