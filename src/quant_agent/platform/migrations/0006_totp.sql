-- T11 · 双因素登录（TOTP）
--
-- 两张表，都带 workspace_id：2FA 的绑定与恢复码都要按工作区隔离，跨工作区不可见
-- （与平台其他表一致）。
--
-- 密钥列存的是 SecretBox（Fernet）密文，不是明文：TOTP 共享密钥属于凭证，
-- 单独泄露一份数据库备份不足以解出它。恢复码同理，只存 SHA-256 哈希。
-- 明文只在"启用"响应里返回一次（用户要扫码 / 抄写恢复码），之后任何查询都不回显。

-- 用户的 TOTP 绑定。一个用户一行，未启用时没有行（而不是 enabled=0 的空行），
-- 这样"是否启用"就是一次存在性判断，不需要区分"没配"和"配了但关着"。
CREATE TABLE user_totp (
  user_id TEXT PRIMARY KEY REFERENCES users(id),
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  secret_enc TEXT NOT NULL,
  enabled_at BIGINT,
  created_at BIGINT NOT NULL,
  last_used_step BIGINT,
  failed_attempts INTEGER NOT NULL DEFAULT 0,
  locked_until BIGINT
);
CREATE INDEX idx_user_totp_ws ON user_totp(workspace_id);

-- 一次性恢复码。used_at 非空即失效；登录成功时用掉哪一条就写哪一条。
-- 恢复码是"验证器丢了"时的唯一出路，所以必须一次性——否则它就成了永久后门口令。
CREATE TABLE totp_recovery_codes (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  code_hash TEXT NOT NULL,
  created_at BIGINT NOT NULL,
  used_at BIGINT
);
CREATE INDEX idx_totp_recovery_user ON totp_recovery_codes(user_id, used_at);
