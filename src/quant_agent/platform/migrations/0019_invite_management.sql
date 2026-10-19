-- 邀请码管理：工作区列表按创建时间查询，并快速筛选仍可使用的单次邀请码。

CREATE INDEX IF NOT EXISTS idx_invites_workspace_created ON invites(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_invites_workspace_state ON invites(workspace_id, revoked_at, used_at, expires_at);
