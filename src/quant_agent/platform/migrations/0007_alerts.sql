-- T09 · 告警与通知
--
-- 为什么用数据库表而不是 JSON 集合：
-- 1) 告警历史需要按工作区 + 时间倒序分页查询，SQL 的索引与 LIMIT 比读整个 JSON 文件更合适；
-- 2) 冷却去重（同一事件 10 分钟内不重复发送）必须有一个"最后发送时间"的权威来源，
--    多个 worker 进程同时触发同一事件时，只有数据库上的条件更新能保证只发一次；
-- 3) 投递重试需要"待投递队列"的持久化状态（尝试次数、下次重试时间、最终失败原因），
--    进程重启后不能丢。
--
-- 凭证约定：webhook URL / bot token / SMTP 密码都是凭证，绝不落在这些表里。
-- 表里只存 secret_ref（指向 SecretBox 加密后的独立文件），接口只回显掩码。
--
-- 时间一律是 UTC 毫秒整数（BIGINT），JSON 存 TEXT，与 0001_init.sql 的约定一致。
-- 金额列用 DECIMAL_T、自增主键用 AUTOINC_PK，由 platform/db.py::_dialect_sql 按方言替换。

-- ---------- 通知渠道（按工作区配置） ----------
-- 一个工作区可以配多条渠道；`kind` 决定用哪个 Notifier 实现。
--
-- 为什么 workspace_id **不加** REFERENCES workspaces(id)：
-- 告警链是旁路（side channel）。如果这里挂外键，任何一次"工作区刚被删/正在建"
-- 的时序竞争都会让 INSERT 失败，而告警失败**不应该**有任何后果之外的副作用——
-- 更糟的是 `raise_alert_safe` 会把它吞掉，于是"通知静默消失"。
-- 仓库里同一性质的表都是这个约定：audit_log、run_events、alerts、alert_deliveries
-- 的 workspace_id 都没有外键。工作区合法性由调用方（已验证的请求上下文）保证。
CREATE TABLE notify_channels (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  created_by TEXT NOT NULL,
  -- console | webhook | email | wecom | serverchan | pushplus
  kind TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  -- 非凭证的通道参数（SMTP 主机/端口/用户名/收件人、webhook 的展示名等）
  config TEXT NOT NULL DEFAULT '{}',
  -- 凭证密文引用；明文只存在于 SecretBox 加密文件里，数据库与接口都看不到
  secret_ref TEXT,
  -- 脱敏回显：形如 "https://qyapi.weixin.qq.com/…a1b2" 或 "ab****yz"
  masked TEXT NOT NULL DEFAULT '',
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL
);
CREATE INDEX idx_notify_channels_ws ON notify_channels(workspace_id, created_at);

-- ---------- 告警规则 ----------
CREATE TABLE alert_rules (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  created_by TEXT NOT NULL,
  name TEXT NOT NULL,
  -- 触发条件：run_failed | paper_risk_pause | readonly_connection_invalid | network_unreachable
  --          | drawdown_exceeded | daily_loss_exceeded | paper_stream_stalled
  event_type TEXT NOT NULL,
  -- 阈值参数（drawdown_pause 比例、单日亏损比例、断流分钟数等）
  params TEXT NOT NULL DEFAULT '{}',
  -- 该规则触发时投递到哪些渠道（渠道 id 列表）
  channel_ids TEXT NOT NULL DEFAULT '[]',
  enabled INTEGER NOT NULL DEFAULT 1,
  -- 冷却窗口（秒）：同一事件在窗口内重复触发只记一次、只发一次
  cooldown_seconds INTEGER NOT NULL DEFAULT 600,
  -- 去重键的维度：rule（同一规则一条）| rule+subject（同一规则同一对象各一条）
  dedupe_scope TEXT NOT NULL DEFAULT 'rule',
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL
);
CREATE INDEX idx_alert_rules_ws ON alert_rules(workspace_id, event_type);

-- ---------- 告警历史 ----------
-- 这张表同时承担两个职责，因此不能删：
--   a) 界面上的告警列表（按 workspace_id + created_at 倒序分页）；
--   b) **冷却去重的权威状态**（按 workspace_id + dedupe_key 找最近一条发送时间）。
-- 把去重状态单独放一张表会带来"两张表不一致"的经典问题（告警写成功、去重表写失败），
-- 所以这里用同一张表：一条 INSERT 同时完成"记一条告警"和"占住冷却窗口"。
CREATE TABLE alerts (
  id AUTOINC_PK,
  workspace_id TEXT NOT NULL,
  -- 命中哪条规则（没有规则时为空：事件发生了但没人订阅，仍要留痕）
  rule_id TEXT,
  event_type TEXT NOT NULL,
  severity TEXT NOT NULL DEFAULT 'warning',
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  -- 触发来源（run_id / session_id / connection_id / provider），用于去重与排查
  subject TEXT NOT NULL DEFAULT '',
  -- 触发时抓取的上下文快照；写入前统一脱敏，绝不含凭证
  context TEXT NOT NULL DEFAULT '{}',
  -- pending（已入队待投递）| sent（全部渠道投递成功）| failed（重试耗尽）
  -- | suppressed（被冷却窗口压住，只留痕不投递）
  status TEXT NOT NULL DEFAULT 'pending',
  -- suppressed 时说明被哪条告警的冷却窗口压住
  suppressed_by BIGINT,
  -- 去重键：默认是规则 id；dedupe_scope=rule+subject 时是 "规则id#对象"
  dedupe_key TEXT NOT NULL DEFAULT '',
  read_at BIGINT,
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL
);
CREATE INDEX idx_alerts_ws_created ON alerts(workspace_id, created_at);
CREATE INDEX idx_alerts_ws_unread ON alerts(workspace_id, read_at);
-- 冷却判定：按 (workspace, dedupe_key, created_at) 找最近一条即可
CREATE INDEX idx_alerts_dedupe ON alerts(workspace_id, dedupe_key, created_at);

-- ---------- 投递队列（入队 + 重试） ----------
-- 为什么单独一张表而不是复用 alerts.status：
-- 一条告警要投递到多个渠道，每个渠道的成败与重试次数是独立的；
-- 把状态放在 alerts 上，一个渠道失败会让整条告警看起来失败。
--
-- 为什么投递必须由 worker 做：
-- 发通知是网络 I/O（企业微信 / Server 酱 / SMTP 都可能超时数秒），
-- 在 API 请求线程里同步发送会把一个 HTTP 请求拖成十几秒。所以 API 只做
-- "校验 + 入队"，真正的 HTTP 调用由 worker 的周期钩子 drain 出来执行。
CREATE TABLE alert_deliveries (
  id AUTOINC_PK,
  alert_id BIGINT NOT NULL,
  workspace_id TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  channel_kind TEXT NOT NULL,
  -- pending | sending | sent | failed
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  -- 指数退避：下次可尝试的时间（毫秒）
  next_attempt_at BIGINT NOT NULL,
  last_error TEXT,
  -- 渠道返回的原始响应摘要（已脱敏、已截断）
  last_response TEXT NOT NULL DEFAULT '',
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL,
  sent_at BIGINT
);
CREATE INDEX idx_alert_deliveries_pending ON alert_deliveries(status, next_attempt_at);
CREATE INDEX idx_alert_deliveries_alert ON alert_deliveries(alert_id);
