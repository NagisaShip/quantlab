-- T01 · 交易所成交的增量同步与分页
--
-- 背景：币安 GET /api/v3/myTrades 与 OKX GET /api/v5/trade/fills-history 都是**单次限量**接口
-- （币安 limit 上限 1000，OKX limit 上限 100）。只拉一页的结果是"看起来同步成功，
-- 实际上最近成交只进来了一部分"，而且下次同步还会用 startTime / begin 把游标往前挪，
-- 于是漏掉的历史成交永远补不回来。修复办法是两件事一起做：
--   1) 客户端按 id 游标翻页拉全（见 live/accounts.py）；
--   2) 每个交易对记一条**同步水位**，下次只从水位之后拉（就是这张表）。
--
-- 为什么水位必须落库而不是放内存：
--   worker 是一个会被重启 / 被杀 / 换机器的进程，水位放内存意味着每次重启
--   都从"最近若干天"重新拉一遍，白白消耗交易所的权重配额，而且多个 worker
--   同时认领同一个连接时会各拉各的、产生重复请求。落库后水位是唯一真相。
--
-- 为什么水位是两条而不是一条：
--   交易所的 id 游标与时间线不是同一个东西。币安的 fromId 是**递增的成交序号**，
--   OKX 的 tradeId 也是递增数字；但两家的"时间戳"都可能出现乱序（同一毫秒的多笔成交、
--   跨分页返回顺序）。只存时间戳会在下一轮把已经拉过的成交再拉一遍；只存 id 又无法
--   覆盖"用户手工补拉某段时间"的场景。所以两条都存：
--     last_fill_id  —— 游标水位，用于增量续拉（第一判据）
--     last_fill_ts  —— 时间水位，用于跨交易所 / 无 id 场景与人工核对
--   增量判定用 last_fill_id 优先，理由是 id 单调，而时间戳不保证单调。
--
-- 为什么 workspace_id **不加** REFERENCES workspaces(id)：
--   与 0007_alerts.sql 的约定一致。这里是同步过程的旁路状态（bookkeeping），
--   同步本身失败不该被一条外键错误掩盖成别的原因；工作区合法性由调用方
--   （已验证的请求上下文 / run 记录）保证。
--
-- 去重靠的是已有的 fills 表：UNIQUE (run_id, fill_key)，fill_key 由 live/service.py
-- 的 _fill_key() 生成——有交易所成交号时是 "x:<tradeId>"。所以"同一笔成交重复拉到"
-- 只会命中唯一键被跳过，不会产生重复行；水位只负责把请求量压下来。

CREATE TABLE fills_sync_watermarks (
  id AUTOINC_PK,
  workspace_id TEXT NOT NULL,
  -- 只读账户连接 id（account_connections.id）。同一交易所的多个连接各自独立水位。
  connection_id TEXT NOT NULL,
  -- 工作区内唯一的交易对标识，形如 BTC/USDT
  symbol TEXT NOT NULL,
  exchange TEXT NOT NULL,
  environment TEXT NOT NULL DEFAULT 'live',
  -- 游标水位：已同步到的交易所成交号。续拉时请求"比它更新"的成交。
  last_fill_id TEXT,
  -- 时间水位：已同步成交里最大的成交时间（UTC 毫秒）
  last_fill_ts BIGINT,
  -- 最近一次同步成功时交易所返回的"本页成交数"，用来观察是否长期顶在单页上限
  last_batch_size INTEGER NOT NULL DEFAULT 0,
  -- 累计成功同步进来的**新**成交条数（重复拉到的不计）
  total_fills INTEGER NOT NULL DEFAULT 0,
  -- 累计请求页数，用来发现"翻页翻不出去"（长期等于最大页数上限说明触顶了）
  total_pages INTEGER NOT NULL DEFAULT 0,
  last_sync_at BIGINT,
  -- 最近一次同步的错误说明（成功时清空）。绝不写凭证。
  last_error TEXT,
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL,
  UNIQUE (workspace_id, connection_id, symbol)
);
CREATE INDEX idx_fills_watermarks_conn ON fills_sync_watermarks(workspace_id, connection_id);
