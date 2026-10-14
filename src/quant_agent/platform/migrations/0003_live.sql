-- 真实记录：资金流水与余额快照（导入或只读同步），与模拟数据完全分开存放
CREATE TABLE live_flows (
  id AUTOINC_PK,
  run_id TEXT NOT NULL REFERENCES runs(id),
  workspace_id TEXT NOT NULL,
  ts BIGINT NOT NULL,
  currency TEXT NOT NULL,
  amount DECIMAL_T NOT NULL,
  kind TEXT NOT NULL,
  ref TEXT NOT NULL,
  UNIQUE (run_id, ref)
);
CREATE INDEX idx_live_flows_run ON live_flows(run_id, ts);

CREATE TABLE balance_snapshots (
  id AUTOINC_PK,
  run_id TEXT NOT NULL REFERENCES runs(id),
  workspace_id TEXT NOT NULL,
  ts BIGINT NOT NULL,
  currency TEXT NOT NULL,
  free DECIMAL_T NOT NULL,
  locked DECIMAL_T NOT NULL,
  source TEXT NOT NULL
);
CREATE INDEX idx_balance_run ON balance_snapshots(run_id, ts);

ALTER TABLE account_connections ADD COLUMN environment TEXT NOT NULL DEFAULT 'live';
ALTER TABLE account_connections ADD COLUMN ip_restricted INTEGER NOT NULL DEFAULT 0;
ALTER TABLE account_connections ADD COLUMN last_error TEXT;
ALTER TABLE account_connections ADD COLUMN sync_run_id TEXT;
ALTER TABLE imports ADD COLUMN digest TEXT;
