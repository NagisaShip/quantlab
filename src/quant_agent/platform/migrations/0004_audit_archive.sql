-- 审计日志归档表：超过保留期的记录移到这里，而不是删除。
-- 审计是"谁做了什么"的证据，DELETE 会让历史不可追溯；归档后主表变小、日常查询变快，
-- 需要时仍可从归档表查回。字段与 audit_log 一致，另加 archived_at 记录归档时间。
CREATE TABLE audit_archive (
  id BIGINT PRIMARY KEY,
  ts BIGINT NOT NULL,
  workspace_id TEXT NOT NULL,
  user_id TEXT,
  action TEXT NOT NULL,
  object_type TEXT NOT NULL DEFAULT '',
  object_id TEXT NOT NULL DEFAULT '',
  trace_id TEXT,
  ip TEXT,
  details TEXT NOT NULL DEFAULT '{}',
  archived_at BIGINT NOT NULL
);

CREATE INDEX idx_audit_archive_ws ON audit_archive(workspace_id, ts);
