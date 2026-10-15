-- 模型调用明细补上输入缓存：缓存命中（cache read）与缓存写入（cache write）的 token 数。
-- input_tokens 仍是全部输入（含缓存部分），两列是其中的拆分，费用按各自单价折算。
ALTER TABLE llm_usage ADD COLUMN cache_read_tokens BIGINT NOT NULL DEFAULT 0;
ALTER TABLE llm_usage ADD COLUMN cache_write_tokens BIGINT NOT NULL DEFAULT 0;
