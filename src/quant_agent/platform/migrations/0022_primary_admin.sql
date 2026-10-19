-- 部署主管理员：最早注册的账号。设置独立标记后，即使之后改 created_at、删除或增加账号，
-- 主管理员身份也保持稳定；对应账号不可被封禁、撤销或降级。
ALTER TABLE users ADD COLUMN is_primary_admin INTEGER NOT NULL DEFAULT 0;

UPDATE users SET is_primary_admin=1
WHERE id = (SELECT id FROM users ORDER BY created_at ASC, id ASC LIMIT 1);
