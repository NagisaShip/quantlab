-- 个人主页展示配置：用户自己决定主页上展示哪些研究成果、展示顺序与条数。
-- 存 JSON：{"items":[{"key":"factors","show":true}, ...],"limit":5}
-- 空串表示还没配置过，读取时按默认（全部展示、每块 5 条）处理。

ALTER TABLE community_profiles ADD COLUMN showcase TEXT NOT NULL DEFAULT '';
