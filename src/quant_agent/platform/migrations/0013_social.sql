-- 策略广场社交关系：用户资料、关注、拉黑与私信策略。
-- 放数据库而不是 JSON：关系需要唯一约束、双向查询和并发安全。

CREATE TABLE community_profiles (
  user_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  bio TEXT NOT NULL DEFAULT '',
  -- reply_only：陌生人可发一条，对方回复后才可继续；open：不限制；none：不允许新私信
  dm_policy TEXT NOT NULL DEFAULT 'reply_only',
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL
);
CREATE INDEX idx_community_profiles_ws ON community_profiles(workspace_id);

CREATE TABLE user_follows (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  follower_id TEXT NOT NULL,
  following_id TEXT NOT NULL,
  created_at BIGINT NOT NULL,
  UNIQUE(workspace_id, follower_id, following_id)
);
CREATE INDEX idx_follows_following ON user_follows(workspace_id, following_id, created_at);
CREATE INDEX idx_follows_follower ON user_follows(workspace_id, follower_id, created_at);

CREATE TABLE user_blocks (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  blocker_id TEXT NOT NULL,
  blocked_id TEXT NOT NULL,
  created_at BIGINT NOT NULL,
  UNIQUE(workspace_id, blocker_id, blocked_id)
);
CREATE INDEX idx_blocks_blocker ON user_blocks(workspace_id, blocker_id, created_at);
CREATE INDEX idx_blocks_blocked ON user_blocks(workspace_id, blocked_id, created_at);
