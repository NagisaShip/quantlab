-- 账号：用户名登录 + 绑定邮箱（邮件验证码验证后才算绑定）。
-- 只用用户名注册的账号，email 列写占位地址 <id>@unbound.invalid（.invalid 是保留域名，永远收不到信），
-- 界面与接口把它当成「未绑定邮箱」。
ALTER TABLE users ADD COLUMN username TEXT;
ALTER TABLE users ADD COLUMN email_verified_at BIGINT;
CREATE UNIQUE INDEX idx_users_username ON users(username);

CREATE TABLE email_codes (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  email TEXT NOT NULL,
  -- bind：绑定 / 更换邮箱
  purpose TEXT NOT NULL DEFAULT 'bind',
  code_hash TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at BIGINT NOT NULL,
  expires_at BIGINT NOT NULL,
  used_at BIGINT
);
CREATE INDEX idx_email_codes_user ON email_codes(user_id, created_at);
