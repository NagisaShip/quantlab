-- 模型调用明细：按「工作区 × 成员 × 服务商 × 模型」记录每次调用的 token 与折算费用。
--
-- 为什么不直接复用每日计量表：
--   限额支持滚动时间段（1 小时、5 小时、1 天、7 天、30 天），按服务商与成员两个维度判定，
--   只能从明细上按时间区间求和；每日计量表只有按日累计的工作区总数。
--
-- 费用存「微元」整数（1 元 = 1,000,000），与每日计量表一样避免数据库侧的浮点累加。
-- 价格与汇率由管理员在服务商上自行填写，记录时按当时的价格折算后落库，
-- 之后改价不会回溯改写历史费用。
CREATE TABLE llm_usage (
  id AUTOINC_PK,
  workspace_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  -- workspace：工作区共享的服务商；personal：成员自己的服务商
  scope TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  input_tokens BIGINT NOT NULL DEFAULT 0,
  output_tokens BIGINT NOT NULL DEFAULT 0,
  cost_micro BIGINT NOT NULL DEFAULT 0,
  created_at BIGINT NOT NULL
);

CREATE INDEX idx_llm_usage_ws_time ON llm_usage(workspace_id, created_at);
CREATE INDEX idx_llm_usage_ws_provider ON llm_usage(workspace_id, provider, created_at);
CREATE INDEX idx_llm_usage_ws_user ON llm_usage(workspace_id, user_id, created_at);
