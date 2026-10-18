-- 成员存储配额：按工作区 + 成员汇总上传原文件与缩略图。
-- 现有 uploads 表已经有 user_id / workspace_id；这里补覆盖索引，
-- 避免每次上传前检查配额或打开“我的用量”时扫描整张表。
CREATE INDEX IF NOT EXISTS idx_uploads_ws_user_created ON uploads(workspace_id, user_id, created_at);