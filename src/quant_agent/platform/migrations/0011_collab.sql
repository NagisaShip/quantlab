-- 工作区协作：群组（讨论、任务摊派、群组模型与限额）、对象可见性、编辑人员标记、
-- 消息通知中心与屏蔽、问题报告与附件、账号封禁。
--
-- 为什么放数据库而不是工作区的 JSON 存储：
--   这些数据跨成员读写（多人同时发消息、领任务、标已读），需要数据库侧的并发安全与按人查询；
--   JSON 集合只能整份读-改-写，并发写会丢数据。

-- ---------- 账号封禁（部署管理员） ----------
ALTER TABLE users ADD COLUMN banned_at BIGINT;
ALTER TABLE users ADD COLUMN banned_reason TEXT;
ALTER TABLE users ADD COLUMN banned_by TEXT;

-- 群组模型的用量要按群组区分（不同群组可以登记同名服务商）
ALTER TABLE llm_usage ADD COLUMN group_id TEXT;
CREATE INDEX idx_llm_usage_group ON llm_usage(group_id, created_at);

-- ---------- 群组 ----------
CREATE TABLE collab_groups (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  -- open：工作区内可发现、可直接加入；private：只能由群主 / 群管理员拉人
  visibility TEXT NOT NULL DEFAULT 'open',
  owner_id TEXT NOT NULL,
  -- {"ws_limits": [...] 工作区管理员给群组分配的共享模型额度（全体群成员合计）,
  --  "member_limits": [...] 群主给每位成员设定的群组模型限额}
  settings TEXT NOT NULL DEFAULT '{}',
  created_at BIGINT NOT NULL,
  archived_at BIGINT
);
CREATE INDEX idx_groups_ws ON collab_groups(workspace_id, created_at);

CREATE TABLE collab_group_members (
  group_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  -- owner | admin | member
  role TEXT NOT NULL DEFAULT 'member',
  joined_at BIGINT NOT NULL,
  last_read_id BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (group_id, user_id)
);
CREATE INDEX idx_group_members_user ON collab_group_members(user_id);

CREATE TABLE collab_messages (
  id AUTOINC_PK,
  group_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  author_id TEXT NOT NULL,
  author_name TEXT NOT NULL DEFAULT '',
  -- user | agent | system
  author_kind TEXT NOT NULL DEFAULT 'user',
  body TEXT NOT NULL DEFAULT '',
  -- 引用：[{type: backtest|strategy|file|post|task|code, id, title, ...}]
  refs TEXT NOT NULL DEFAULT '[]',
  reply_to BIGINT,
  pinned INTEGER NOT NULL DEFAULT 0,
  created_at BIGINT NOT NULL,
  edited_at BIGINT,
  deleted_at BIGINT
);
CREATE INDEX idx_messages_group ON collab_messages(group_id, id);

CREATE TABLE collab_tasks (
  id TEXT PRIMARY KEY,
  group_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  assignee_id TEXT,
  created_by TEXT NOT NULL,
  -- todo | doing | review | done | cancelled
  status TEXT NOT NULL DEFAULT 'todo',
  -- low | normal | high | urgent
  priority TEXT NOT NULL DEFAULT 'normal',
  due_at BIGINT,
  refs TEXT NOT NULL DEFAULT '[]',
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL,
  done_at BIGINT
);
CREATE INDEX idx_tasks_group ON collab_tasks(group_id, status);
CREATE INDEX idx_tasks_assignee ON collab_tasks(assignee_id, status);

-- ---------- 对象可见性：策略 / 研究项目是否在工作区或群组内公开 ----------
CREATE TABLE collab_shares (
  workspace_id TEXT NOT NULL,
  object_type TEXT NOT NULL,
  object_id TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  -- private：仅自己（与工作区管理员）；workspace：工作区内公开；group：指定群组内公开
  visibility TEXT NOT NULL DEFAULT 'workspace',
  group_ids TEXT NOT NULL DEFAULT '[]',
  updated_at BIGINT NOT NULL,
  PRIMARY KEY (workspace_id, object_type, object_id)
);

-- ---------- 编辑人员标记（谁在什么时候改了哪个版本，血缘图的边也来自这里） ----------
CREATE TABLE collab_edits (
  id AUTOINC_PK,
  workspace_id TEXT NOT NULL,
  object_type TEXT NOT NULL,
  object_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  user_name TEXT NOT NULL DEFAULT '',
  -- human | agent
  actor_kind TEXT NOT NULL DEFAULT 'human',
  -- create | edit | clone | backtest | approve | share
  action TEXT NOT NULL,
  version TEXT NOT NULL DEFAULT '',
  parent_version TEXT NOT NULL DEFAULT '',
  -- 克隆 / 派生自哪个对象：object_type:object_id
  derived_from TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  created_at BIGINT NOT NULL
);
CREATE INDEX idx_edits_obj ON collab_edits(workspace_id, object_type, object_id, id);

-- ---------- 消息通知中心与屏蔽 ----------
CREATE TABLE notifications_inbox (
  id AUTOINC_PK,
  user_id TEXT NOT NULL,
  workspace_id TEXT,
  -- mention | task | group | comment | like | issue | announce | system | share
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  link TEXT NOT NULL DEFAULT '',
  actor_id TEXT,
  actor_name TEXT NOT NULL DEFAULT '',
  source_type TEXT NOT NULL DEFAULT '',
  source_id TEXT NOT NULL DEFAULT '',
  created_at BIGINT NOT NULL,
  read_at BIGINT
);
CREATE INDEX idx_inbox_user ON notifications_inbox(user_id, read_at, id);

CREATE TABLE notification_mutes (
  user_id TEXT NOT NULL,
  -- user：屏蔽某人；group：屏蔽某群组；kind：屏蔽一类通知；post：屏蔽某帖子的回复
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  created_at BIGINT NOT NULL,
  PRIMARY KEY (user_id, target_type, target_id)
);

-- ---------- 问题报告 ----------
CREATE TABLE issue_reports (
  id TEXT PRIMARY KEY,
  workspace_id TEXT,
  user_id TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  -- bug | feature | question | data | other
  category TEXT NOT NULL DEFAULT 'bug',
  -- low | normal | high | critical
  severity TEXT NOT NULL DEFAULT 'normal',
  -- open | replied | resolved | closed
  status TEXT NOT NULL DEFAULT 'open',
  attachments TEXT NOT NULL DEFAULT '[]',
  context TEXT NOT NULL DEFAULT '{}',
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL
);
CREATE INDEX idx_issues_user ON issue_reports(user_id, created_at);
CREATE INDEX idx_issues_status ON issue_reports(status, updated_at);

CREATE TABLE issue_replies (
  id AUTOINC_PK,
  issue_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  user_name TEXT NOT NULL DEFAULT '',
  is_staff INTEGER NOT NULL DEFAULT 0,
  body TEXT NOT NULL DEFAULT '',
  attachments TEXT NOT NULL DEFAULT '[]',
  created_at BIGINT NOT NULL
);
CREATE INDEX idx_issue_replies ON issue_replies(issue_id, id);

-- ---------- 上传的文件（问题报告截图、群聊附件） ----------
CREATE TABLE uploads (
  id TEXT PRIMARY KEY,
  workspace_id TEXT,
  user_id TEXT NOT NULL,
  filename TEXT NOT NULL,
  mime TEXT NOT NULL,
  size BIGINT NOT NULL,
  sha256 TEXT NOT NULL,
  created_at BIGINT NOT NULL
)
