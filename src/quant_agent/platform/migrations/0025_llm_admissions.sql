-- Pending model requests count against admission limits until confirmed settlement.
CREATE TABLE llm_admissions (
  request_id TEXT PRIMARY KEY,
  run_id TEXT,
  step_id TEXT,
  attempt_id TEXT,
  workspace_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  scope TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  group_id TEXT,
  reserved_tokens BIGINT NOT NULL,
  reserved_micro BIGINT NOT NULL,
  actual_tokens BIGINT,
  actual_micro BIGINT,
  over_tokens BIGINT NOT NULL DEFAULT 0,
  over_micro BIGINT NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  created_at BIGINT NOT NULL,
  settled_at BIGINT
);
CREATE INDEX idx_llm_admissions_quota ON llm_admissions(workspace_id,provider,scope,status,created_at);
ALTER TABLE llm_usage ADD COLUMN admission_id TEXT;
CREATE UNIQUE INDEX idx_llm_usage_admission ON llm_usage(admission_id);
