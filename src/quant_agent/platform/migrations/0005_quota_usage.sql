-- T07 配额计量：按「工作区 × 自然日 × 计量项」累计资源用量。
--
-- 为什么单独建一张表，而不是把计数塞进 workspaces.settings（JSON）：
--   1. 计量写入发生在 worker 与 API 两个进程里，必须靠数据库侧原子自增保证并发下不丢更新；
--      JSON 字段只能"读-改-写"，多 worker 同时加计数必然丢计数。
--   2. 用量是逐日滚动的历史数据，需要按天查询；塞进设置里会让设置行无限膨胀。
--
-- 为什么 value 用 BIGINT 存"细粒度整数"，而不是用金额列类型（重要）：
--   金额列类型在 SQLite 下会落成 TEXT，而 SQLite 对 TEXT 列做 `value = value + ?`
--   会退化成 REAL（二进制浮点）运算——实测在数值较大时会直接丢精度，并写成
--   `1.23456789012346e+19` 这种科学计数法，之后无论是展示还是"是否超限"的比较都会错。
--   而配额判定是硬边界，绝不能用会漂移的表示。
--   因此这里存细粒度整数（单位比展示单位更细，见 platform/quota.py 的 Metric.scale）：
--   CPU 秒存毫秒、字节存字节、次数存次。整数加法在 SQLite 与 PostgreSQL 上都是精确的；
--   而在 Python 与 JSON 边界上仍然是 Decimal（对外输出十进制字符串），不引入 float 记账。
--
-- 注意：本文件里的占位符（金额列类型、自增主键）会被 db._dialect_sql 做**字面替换**，
-- 连注释一起换。所以注释里不要出现那两个占位符本身，否则注释会被改得读不通。
--
-- 唯一键 (workspace_id, day, metric) 是并发安全的基础：
--   同一天同一计量项只有一行，`ON CONFLICT DO NOTHING` + `SET value = value + ?` 才能原子执行。
CREATE TABLE quota_usage (
  id AUTOINC_PK,
  workspace_id TEXT NOT NULL,
  -- 自然日，UTC，格式 YYYY-MM-DD；空串表示"累计口径"（存量指标，如存储占用）。
  -- 用日字符串而不是时间戳：配额是"每日"语义，直接以日界为键，不需要做时区换算。
  day TEXT NOT NULL,
  -- 计量项名，见 platform/quota.py::METRICS。
  metric TEXT NOT NULL,
  -- 已用量，细粒度整数（单位由计量项决定，见 quota.py 的 Metric.scale）。
  value BIGINT NOT NULL DEFAULT 0,
  updated_at BIGINT NOT NULL,
  UNIQUE (workspace_id, day, metric)
);

-- 用量面板按工作区 + 日期区间读取，这个索引正好覆盖该查询。
CREATE INDEX idx_quota_usage_ws_day ON quota_usage(workspace_id, day);
