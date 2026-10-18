-- 策略广场帖子举报：成员提交、工作区管理员审核。
-- 单独建表而不是塞进帖子 JSON：需要按状态查询、去重、审核留痕和并发安全。
CREATE TABLE post_reports (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  post_id TEXT NOT NULL,
  reporter_id TEXT NOT NULL,
  -- spam | abuse | misinformation | duplicate | other
  reason TEXT NOT NULL DEFAULT 'other',
  detail TEXT NOT NULL DEFAULT '',
  -- open | resolved | dismissed
  status TEXT NOT NULL DEFAULT 'open',
  action_taken TEXT NOT NULL DEFAULT '',
  resolution TEXT NOT NULL DEFAULT '',
  reviewed_by TEXT,
  reviewed_at BIGINT,
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL,
  UNIQUE (workspace_id, post_id, reporter_id)
);
CREATE INDEX idx_post_reports_ws_status ON post_reports(workspace_id, status, created_at);
CREATE INDEX idx_post_reports_post ON post_reports(workspace_id, post_id, created_at);