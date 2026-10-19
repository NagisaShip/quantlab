-- 邀请码支持多人使用：保留首名邀请人字段以兼容旧数据，同时记录每次兑换明细。

ALTER TABLE invites ADD COLUMN max_uses INTEGER NOT NULL DEFAULT 1;
ALTER TABLE invites ADD COLUMN used_count INTEGER NOT NULL DEFAULT 0;

UPDATE invites SET used_count = 1 WHERE used_by IS NOT NULL AND used_count = 0;

CREATE TABLE invite_uses (
  id AUTOINC_PK,
  invite_id TEXT NOT NULL REFERENCES invites(id),
  user_id TEXT NOT NULL REFERENCES users(id),
  used_at BIGINT NOT NULL,
  UNIQUE(invite_id, user_id)
);
CREATE INDEX idx_invite_uses_invite ON invite_uses(invite_id, used_at DESC);

INSERT INTO invite_uses (invite_id, user_id, used_at)
SELECT id, used_by, used_at FROM invites WHERE used_by IS NOT NULL AND used_at IS NOT NULL;
