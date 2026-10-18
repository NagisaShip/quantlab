-- 统一头像、图片缩略图与跨内容收藏。

ALTER TABLE community_profiles ADD COLUMN avatar_color TEXT NOT NULL DEFAULT '#2E5C9A';

ALTER TABLE uploads ADD COLUMN thumb_mime TEXT;
ALTER TABLE uploads ADD COLUMN thumb_size BIGINT;
ALTER TABLE uploads ADD COLUMN width INTEGER;
ALTER TABLE uploads ADD COLUMN height INTEGER;

CREATE TABLE favorites (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  -- post | strategy | backtest | factor | model | project | link
  object_type TEXT NOT NULL,
  object_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  link TEXT NOT NULL DEFAULT '',
  meta TEXT NOT NULL DEFAULT '{}',
  created_at BIGINT NOT NULL,
  UNIQUE(workspace_id, user_id, object_type, object_id)
);
CREATE INDEX idx_favorites_user ON favorites(workspace_id, user_id, created_at);
