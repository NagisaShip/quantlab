-- worker 存活登记：健康页显示在线 worker、各自认领的任务数与最后心跳
CREATE TABLE workers (
  id TEXT PRIMARY KEY,
  host TEXT NOT NULL,
  pid INTEGER NOT NULL,
  kinds TEXT NOT NULL DEFAULT '[]',
  started_at BIGINT NOT NULL,
  last_seen BIGINT NOT NULL,
  info TEXT NOT NULL DEFAULT '{}'
);

-- 运行的错误码（与 API 错误契约一致），便于前端按类别提示
ALTER TABLE runs ADD COLUMN error_code TEXT;
