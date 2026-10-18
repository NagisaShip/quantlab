-- 群聊消息的轻量 Qmoji Reaction：一条消息可以被多个成员贴多个表情。
-- 只存 (message_id, user_id, emoji) 的唯一关系，计数在查询时聚合；删除消息时保留关系但详情不再展示。
CREATE TABLE collab_message_reactions (
  message_id BIGINT NOT NULL,
  user_id TEXT NOT NULL,
  emoji TEXT NOT NULL,
  created_at BIGINT NOT NULL,
  PRIMARY KEY (message_id, user_id, emoji)
);
CREATE INDEX idx_message_reactions_message ON collab_message_reactions(message_id);
