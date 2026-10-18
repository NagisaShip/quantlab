-- QuantLab 公开身份与未登录预览：
-- 1) 用户拥有唯一 UID，群组拥有唯一群号，均可重新生成（旧号码立即失效）；
-- 2) 用户资料显式公开后才会出现在目录搜索中；
-- 3) 公开分享使用不可猜测的 token，撤销后旧链接立即失效。

ALTER TABLE users ADD COLUMN quantlab_uid TEXT;
ALTER TABLE collab_groups ADD COLUMN quantlab_no TEXT;
ALTER TABLE community_profiles ADD COLUMN public_profile INTEGER NOT NULL DEFAULT 0;

UPDATE users
SET quantlab_uid = 'QLU-' || RANDOM_HEX_8
WHERE quantlab_uid IS NULL OR quantlab_uid = '';

UPDATE collab_groups
SET quantlab_no = 'QLG-' || RANDOM_HEX_8
WHERE (quantlab_no IS NULL OR quantlab_no = '')
  AND settings NOT LIKE '%"dm"%';

CREATE UNIQUE INDEX idx_users_quantlab_uid ON users(quantlab_uid) WHERE quantlab_uid IS NOT NULL;
CREATE UNIQUE INDEX idx_groups_quantlab_no ON collab_groups(quantlab_no) WHERE quantlab_no IS NOT NULL;
CREATE INDEX idx_profiles_public ON community_profiles(workspace_id, public_profile);

CREATE TABLE public_shares (
  token TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  object_type TEXT NOT NULL,
  object_id TEXT NOT NULL,
  created_by TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  route TEXT NOT NULL DEFAULT '',
  created_at BIGINT NOT NULL,
  revoked_at BIGINT
);
CREATE INDEX idx_public_shares_object ON public_shares(workspace_id, object_type, object_id, revoked_at);
CREATE UNIQUE INDEX idx_public_shares_active ON public_shares(workspace_id, object_type, object_id)
  WHERE revoked_at IS NULL;
