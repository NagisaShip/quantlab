-- Runtime-owned agent execution: legacy runs are ingress tickets and API projections.
ALTER TABLE runs ADD COLUMN runtime_version TEXT;
ALTER TABLE runs ADD COLUMN runtime_event_seq BIGINT NOT NULL DEFAULT 0;

CREATE TABLE runtime_tickets (
  run_id TEXT PRIMARY KEY REFERENCES runs(id),
  runtime_version TEXT NOT NULL,
  prompt TEXT NOT NULL,
  spec TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at BIGINT NOT NULL,
  delivered_at BIGINT
);
