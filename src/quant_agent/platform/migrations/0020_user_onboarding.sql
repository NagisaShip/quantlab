-- 新手引导：每个用户一份，记录首次注册后的步骤进度。
-- 已有用户迁移时直接标记为完成，避免上线后所有老用户被弹窗打断。

CREATE TABLE user_onboarding (
  user_id TEXT PRIMARY KEY REFERENCES users(id),
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'not_started',
  current_step TEXT NOT NULL DEFAULT 'welcome',
  track TEXT,
  completed_steps TEXT NOT NULL DEFAULT '[]',
  started_at BIGINT,
  completed_at BIGINT,
  updated_at BIGINT NOT NULL
);

INSERT INTO user_onboarding
  (user_id, version, status, current_step, track, completed_steps, started_at, completed_at, updated_at)
SELECT
  id,
  1,
  'completed',
  'finish',
  CASE WHEN is_admin = 1 THEN 'owner' ELSE 'member' END,
  '["welcome","flow","goal","agents","backtest","finish"]',
  created_at,
  created_at,
  created_at
FROM users;
