# QuantLab v0.1.0-preview.1 · API Reference

> The machine-readable contract is [`openapi.json`](openapi.json). The complete operation-by-operation reference is generated in Chinese at [`API参考.md`](API参考.md). A running instance exposes interactive Swagger UI at `/docs` and ReDoc at `/redoc`.

[中文](API参考.md) | [Documentation index](README.en.md) | [Back to README](../README.md)

## Scope

The current snapshot contains **604 operations across 513 paths**. During the preview period, endpoint details may change before a stable release.

## Base URL and authentication

- Same-origin API prefix: `/api`.
- Browser sessions use the HttpOnly cookie `qa_session`.
- API clients may use `Authorization: Bearer <token>` where supported.
- Public endpoints include login, registration status, `/api/health` and `/api/healthz`; most other endpoints require authentication.
- Workspace-scoped requests send `X-Workspace-Id`. The server validates membership on every request.
- State-changing requests must send `X-Requested-With`.
- Write operations that create resources should send an `Idempotency-Key`.
- Reviewers are read-only.

Example:

```bash
curl -H 'X-Requested-With: api'      -H 'X-Workspace-Id: ws-...'      -b 'qa_session=...'      http://127.0.0.1:8000/api/runs
```

## Data conventions

- Money, prices and quantities are decimal strings in JSON.
- Timestamps are Unix milliseconds unless an endpoint explicitly states otherwise.
- Strategy, data, parameter and tool calls are recorded for reproducibility.
- Capability switches in the workspace can reject a request even if the route exists.

## Error contract

Errors use a stable envelope:

```json
{
  "code": "PERMISSION_DENIED",
  "message": "Current workspace has disabled this feature",
  "trace_id": "...",
  "details": {"feature": "execution.real_perp"}
}
```

A `403 PERMISSION_DENIED` can mean either a workspace membership failure or a disabled feature. Clients should inspect `details`; the bundled web client clears the selected workspace only when `details.workspace_id` is present.

## Main endpoint groups

| Area | Representative paths |
|---|---|
| Accounts and workspaces | `/api/auth/*`, `/api/workspaces`, `/api/workspace/features`, `/api/workspace/settings` |
| Market data | `/api/market/*`, `/api/datahub/*`, `/api/imports` |
| DSL and strategies | `/api/dsl/*`, `/api/studio/strategies/*`, `/api/strategy-versions/*` |
| Backtests and runs | `/api/research/backtests`, `/api/runs/*`, `/api/runs/workers` |
| Validation | `/api/validation/*`, `/api/graduation/*` |
| Factors and models | `/api/factors/*`, `/api/models/*`, `/api/modellib/*` |
| Portfolios and execution | `/api/portfolio/*`, `/api/execution/*`, `/api/comparisons` |
| Strategy packages | `/api/studio/packages/*`, `/api/freqtrade/*` |
| AI Lab and Agents | `/api/lab/*`, `/api/agents/*`, `/api/ai/*`, `/api/mentor/*` |
| Paper/demo/live | `/api/studio/paper/*`, `/api/demo/*`, `/api/live/*`, `/api/accounts/*` |
| Micro live and real perpetuals | `/api/microlive/*`, `/api/realperp/*` |
| Research quality | `/api/capabilities`, `/api/research/review*`, `/api/research/packages*` |
| Collaboration | `/api/collab/*`, `/api/community/*`, `/api/inbox/*` |
| Operations | `/api/ops/*`, `/api/admin/*`, `/api/runs/*/commands` |

## Long-running jobs

Backtests, optimization, stress drills, model training and comparison jobs return a run identifier. Poll:

```text
GET /api/runs/{run_id}
GET /api/runs/{run_id}/events
GET /api/runs/{run_id}/result
```

Events are resumable through the `after` cursor. WebSocket and SSE endpoints provide live updates. Run state must be interpreted with the documented state machine; do not edit database state directly while workers are active.

## Reproducibility and research packages

Research-package APIs export code, data references, funding data, prompts, model version, tool calls, parameters, results and the review report. Import/replay verifies hashes and reports mismatches rather than silently accepting a changed experiment.

## Real-money APIs

Micro-live and real-perpetual routes require all deployment, workspace and per-start gates. Feature discovery alone does not mean the channel is enabled. Clients must display the server-returned blockers and must never cache a previous confirmation as approval for a new start.

## Discovering exact schemas

1. Open `/docs` on the running server for interactive request/response schemas.
2. Inspect [`openapi.json`](openapi.json) for generated clients or contract tests.
3. Use the Chinese [`API参考.md`](API参考.md) for the full path-by-path operation index.

## Complete endpoint index

Generated from `openapi.json`. Each operation ID maps to a handler and schema in the interactive `/docs` UI.

| Method | Path | Operation ID |
|---|---|---|
| `GET` | `/api/accounts/connections` | `list_connections_api_accounts_connections_get` |
| `POST` | `/api/accounts/connections` | `connect_api_accounts_connections_post` |
| `DELETE` | `/api/accounts/connections/{cid}` | `revoke_connection_api_accounts_connections__cid__delete` |
| `POST` | `/api/accounts/connections/{cid}/sync` | `sync_connection_api_accounts_connections__cid__sync_post` |
| `POST` | `/api/accounts/connections/{cid}/test` | `test_connection_api_accounts_connections__cid__test_post` |
| `GET` | `/api/accounts/connections/{cid}/watermarks` | `connection_watermarks_api_accounts_connections__cid__watermarks_get` |
| `GET` | `/api/accounts/guide` | `account_guide_api_accounts_guide_get` |
| `POST` | `/api/admin/announce` | `announce_api_admin_announce_post` |
| `GET` | `/api/admin/audit` | `audit_api_admin_audit_get` |
| `GET` | `/api/admin/overview` | `overview_api_admin_overview_get` |
| `GET` | `/api/admin/settings` | `get_settings_api_admin_settings_get` |
| `PUT` | `/api/admin/settings` | `put_settings_api_admin_settings_put` |
| `GET` | `/api/admin/usage` | `usage_api_admin_usage_get` |
| `GET` | `/api/admin/users` | `list_users_api_admin_users_get` |
| `POST` | `/api/admin/users` | `create_user_api_admin_users_post` |
| `GET` | `/api/admin/users/{uid}` | `user_detail_api_admin_users__uid__get` |
| `POST` | `/api/admin/users/{uid}/admin` | `set_admin_api_admin_users__uid__admin_post` |
| `POST` | `/api/admin/users/{uid}/ban` | `ban_user_api_admin_users__uid__ban_post` |
| `POST` | `/api/admin/users/{uid}/reset-password` | `reset_password_api_admin_users__uid__reset_password_post` |
| `POST` | `/api/admin/users/{uid}/sessions/revoke` | `revoke_sessions_api_admin_users__uid__sessions_revoke_post` |
| `POST` | `/api/admin/users/{uid}/unban` | `unban_user_api_admin_users__uid__unban_post` |
| `GET` | `/api/admin/workspaces` | `list_workspaces_api_admin_workspaces_get` |
| `PUT` | `/api/admin/workspaces/{wid}/status` | `set_ws_status_api_admin_workspaces__wid__status_put` |
| `GET` | `/api/agents/prompts` | `list_prompts_api_agents_prompts_get` |
| `GET` | `/api/agents/roles` | `list_roles_api_agents_roles_get` |
| `POST` | `/api/agents/roles` | `create_role_api_agents_roles_post` |
| `POST` | `/api/agents/roles/draft` | `draft_role_api_agents_roles_draft_post` |
| `POST` | `/api/agents/roles/duplicate/{role_id}` | `duplicate_role_api_agents_roles_duplicate__role_id__post` |
| `DELETE` | `/api/agents/roles/{role_id}` | `delete_role_api_agents_roles__role_id__delete` |
| `PUT` | `/api/agents/roles/{role_id}` | `update_role_api_agents_roles__role_id__put` |
| `POST` | `/api/agents/roles/{role_id}/reset` | `reset_role_api_agents_roles__role_id__reset_post` |
| `POST` | `/api/agents/roles/{role_id}/run` | `run_single_role_api_agents_roles__role_id__run_post` |
| `GET` | `/api/agents/store-stats` | `store_stats_api_agents_store_stats_get` |
| `GET` | `/api/agents/team-runs` | `list_team_runs_api_agents_team_runs_get` |
| `GET` | `/api/agents/team-runs/{run_id}` | `get_team_run_api_agents_team_runs__run_id__get` |
| `GET` | `/api/agents/teams` | `list_teams_api_agents_teams_get` |
| `POST` | `/api/agents/teams` | `create_team_api_agents_teams_post` |
| `DELETE` | `/api/agents/teams/{team_id}` | `delete_team_api_agents_teams__team_id__delete` |
| `PUT` | `/api/agents/teams/{team_id}` | `update_team_api_agents_teams__team_id__put` |
| `POST` | `/api/agents/teams/{team_id}/run` | `run_team_api_agents_teams__team_id__run_post` |
| `GET` | `/api/ai/actions/{action_id}` | `ai_action_get_api_ai_actions__action_id__get` |
| `POST` | `/api/ai/actions/{action_id}` | `ai_action_decide_api_ai_actions__action_id__post` |
| `POST` | `/api/ai/ask` | `ask_api_ai_ask_post` |
| `GET` | `/api/ai/ask/meta` | `ask_meta_api_ai_ask_meta_get` |
| `POST` | `/api/ai/ask/stream` | `ask_stream_api_ai_ask_stream_post` |
| `POST` | `/api/ai/chat` | `ai_chat_api_ai_chat_post` |
| `POST` | `/api/ai/edit` | `ai_edit_api_ai_edit_post` |
| `POST` | `/api/ai/explain` | `ai_explain_api_ai_explain_post` |
| `POST` | `/api/ai/fix` | `ai_fix_api_ai_fix_post` |
| `POST` | `/api/ai/generate` | `ai_generate_api_ai_generate_post` |
| `GET` | `/api/ai/history` | `ai_history_api_ai_history_get` |
| `POST` | `/api/ai/interpret` | `ai_interpret_api_ai_interpret_post` |
| `POST` | `/api/ai/review` | `ai_review_api_ai_review_post` |
| `GET` | `/api/ai/tools` | `ai_tools_api_ai_tools_get` |
| `GET` | `/api/alert-rules` | `list_alert_rules_api_alert_rules_get` |
| `POST` | `/api/alert-rules` | `save_alert_rule_api_alert_rules_post` |
| `DELETE` | `/api/alert-rules/{rule_id}` | `delete_alert_rule_api_alert_rules__rule_id__delete` |
| `GET` | `/api/alerts` | `list_alerts_api_alerts_get` |
| `POST` | `/api/alerts/preview` | `preview_alert_api_alerts_preview_post` |
| `POST` | `/api/alerts/read` | `mark_alerts_read_api_alerts_read_post` |
| `GET` | `/api/alerts/status` | `alerts_status_api_alerts_status_get` |
| `POST` | `/api/alerts/test` | `test_alert_api_alerts_test_post` |
| `GET` | `/api/arena/batches` | `list_batches_api_arena_batches_get` |
| `GET` | `/api/arena/batches/{batch_id}` | `get_batch_api_arena_batches__batch_id__get` |
| `GET` | `/api/arena/batches/{batch_id}/leaderboard` | `batch_leaderboard_api_arena_batches__batch_id__leaderboard_get` |
| `GET` | `/api/arena/datasets` | `list_datasets_api_arena_datasets_get` |
| `GET` | `/api/arena/datasets/sample.csv` | `download_sample_api_arena_datasets_sample_csv_get` |
| `POST` | `/api/arena/datasets/upload` | `upload_dataset_api_arena_datasets_upload_post` |
| `DELETE` | `/api/arena/datasets/{dataset_id}` | `delete_dataset_api_arena_datasets__dataset_id__delete` |
| `GET` | `/api/arena/rules` | `list_rules_api_arena_rules_get` |
| `GET` | `/api/arena/rules/meta` | `rules_meta_api_arena_rules_meta_get` |
| `POST` | `/api/arena/rules/save` | `save_rule_api_arena_rules_save_post` |
| `POST` | `/api/arena/rules/validate` | `validate_rule_api_arena_rules_validate_post` |
| `POST` | `/api/arena/run` | `run_arena_api_arena_run_post` |
| `GET` | `/api/arena/track/defaults` | `track_defaults_api_arena_track_defaults_get` |
| `POST` | `/api/arena/track/validate` | `validate_track_api_arena_track_validate_post` |
| `GET` | `/api/arena/windows/meta` | `windows_meta_api_arena_windows_meta_get` |
| `GET` | `/api/auth/account` | `get_account_api_auth_account_get` |
| `PUT` | `/api/auth/account` | `put_account_api_auth_account_put` |
| `POST` | `/api/auth/email/code` | `send_email_code_api_auth_email_code_post` |
| `POST` | `/api/auth/email/verify` | `verify_email_api_auth_email_verify_post` |
| `POST` | `/api/auth/login` | `login_api_auth_login_post` |
| `POST` | `/api/auth/logout` | `logout_api_auth_logout_post` |
| `GET` | `/api/auth/me` | `me_api_auth_me_get` |
| `POST` | `/api/auth/password` | `change_password_api_auth_password_post` |
| `POST` | `/api/auth/quantlab-id/rotate` | `rotate_quantlab_uid_api_auth_quantlab_id_rotate_post` |
| `POST` | `/api/auth/register` | `register_api_auth_register_post` |
| `GET` | `/api/auth/status` | `auth_status_api_auth_status_get` |
| `GET` | `/api/auth/totp` | `totp_status_api_auth_totp_get` |
| `POST` | `/api/auth/totp/disable` | `totp_disable_api_auth_totp_disable_post` |
| `POST` | `/api/auth/totp/enable` | `totp_enable_api_auth_totp_enable_post` |
| `POST` | `/api/auth/totp/recovery-codes` | `totp_recovery_codes_api_auth_totp_recovery_codes_post` |
| `POST` | `/api/auth/totp/setup` | `totp_setup_api_auth_totp_setup_post` |
| `POST` | `/api/backtests` | `run_backtest_api_backtests_post` |
| `GET` | `/api/bot` | `bot_list_api_bot_get` |
| `POST` | `/api/bot` | `bot_create_api_bot_post` |
| `POST` | `/api/bot/bind-code` | `bot_bind_code_api_bot_bind_code_post` |
| `DELETE` | `/api/bot/bindings/{binding_id}` | `bot_unbind_api_bot_bindings__binding_id__delete` |
| `POST` | `/api/bot/hook/feishu/{bot_id}` | `hook_feishu_api_bot_hook_feishu__bot_id__post` |
| `GET` | `/api/bot/hook/wecom/{bot_id}` | `hook_wecom_verify_api_bot_hook_wecom__bot_id__get` |
| `POST` | `/api/bot/hook/wecom/{bot_id}` | `hook_wecom_api_bot_hook_wecom__bot_id__post` |
| `DELETE` | `/api/bot/{bot_id}` | `bot_delete_api_bot__bot_id__delete` |
| `GET` | `/api/capabilities` | `capabilities_api_capabilities_get` |
| `POST` | `/api/capabilities/contracts/verify-bills` | `contract_verify_bills_api_capabilities_contracts_verify_bills_post` |
| `GET` | `/api/capabilities/contracts/{exchange}/{symbol}` | `contract_spec_api_capabilities_contracts__exchange___symbol__get` |
| `GET` | `/api/collab/discover` | `discover_api_collab_discover_get` |
| `POST` | `/api/collab/dm` | `open_dm_api_collab_dm_post` |
| `GET` | `/api/collab/editors/{object_type}` | `editors_api_collab_editors__object_type__get` |
| `GET` | `/api/collab/edits/{object_type}/{object_id}` | `edit_log_api_collab_edits__object_type___object_id__get` |
| `GET` | `/api/collab/groups` | `list_groups_api_collab_groups_get` |
| `POST` | `/api/collab/groups` | `create_group_api_collab_groups_post` |
| `DELETE` | `/api/collab/groups/{gid}` | `archive_group_api_collab_groups__gid__delete` |
| `GET` | `/api/collab/groups/{gid}` | `get_group_api_collab_groups__gid__get` |
| `PUT` | `/api/collab/groups/{gid}` | `update_group_api_collab_groups__gid__put` |
| `POST` | `/api/collab/groups/{gid}/join` | `join_group_api_collab_groups__gid__join_post` |
| `POST` | `/api/collab/groups/{gid}/members` | `add_members_api_collab_groups__gid__members_post` |
| `DELETE` | `/api/collab/groups/{gid}/members/{user_id}` | `remove_member_api_collab_groups__gid__members__user_id__delete` |
| `PUT` | `/api/collab/groups/{gid}/members/{user_id}` | `set_member_role_api_collab_groups__gid__members__user_id__put` |
| `GET` | `/api/collab/groups/{gid}/messages` | `list_messages_api_collab_groups__gid__messages_get` |
| `POST` | `/api/collab/groups/{gid}/messages` | `post_message_api_collab_groups__gid__messages_post` |
| `POST` | `/api/collab/groups/{gid}/number/rotate` | `rotate_group_number_api_collab_groups__gid__number_rotate_post` |
| `PUT` | `/api/collab/groups/{gid}/quota` | `set_group_quota_api_collab_groups__gid__quota_put` |
| `POST` | `/api/collab/groups/{gid}/read` | `mark_read_api_collab_groups__gid__read_post` |
| `GET` | `/api/collab/groups/{gid}/shared` | `group_shared_api_collab_groups__gid__shared_get` |
| `GET` | `/api/collab/groups/{gid}/tasks` | `group_tasks_api_collab_groups__gid__tasks_get` |
| `POST` | `/api/collab/groups/{gid}/tasks` | `create_task_api_collab_groups__gid__tasks_post` |
| `GET` | `/api/collab/groups/{gid}/usage` | `group_usage_api_collab_groups__gid__usage_get` |
| `GET` | `/api/collab/lineage` | `lineage_api_collab_lineage_get` |
| `GET` | `/api/collab/members` | `members_api_collab_members_get` |
| `DELETE` | `/api/collab/messages/{mid}` | `delete_message_api_collab_messages__mid__delete` |
| `PUT` | `/api/collab/messages/{mid}` | `edit_message_api_collab_messages__mid__put` |
| `POST` | `/api/collab/messages/{mid}/pin` | `pin_message_api_collab_messages__mid__pin_post` |
| `POST` | `/api/collab/messages/{mid}/qmoji` | `qmoji_message_api_collab_messages__mid__qmoji_post` |
| `POST` | `/api/collab/presence` | `presence_api_collab_presence_post` |
| `POST` | `/api/collab/public-shares` | `create_public_share_api_collab_public_shares_post` |
| `DELETE` | `/api/collab/public-shares/{token}` | `revoke_public_share_api_collab_public_shares__token__delete` |
| `GET` | `/api/collab/refs/search` | `search_refs_api_collab_refs_search_get` |
| `POST` | `/api/collab/share-to` | `share_to_api_collab_share_to_post` |
| `GET` | `/api/collab/shares/{object_type}/{object_id}` | `get_share_api_collab_shares__object_type___object_id__get` |
| `PUT` | `/api/collab/shares/{object_type}/{object_id}` | `put_share_api_collab_shares__object_type___object_id__put` |
| `GET` | `/api/collab/tasks/mine` | `my_tasks_api_collab_tasks_mine_get` |
| `DELETE` | `/api/collab/tasks/{tid}` | `delete_task_api_collab_tasks__tid__delete` |
| `PUT` | `/api/collab/tasks/{tid}` | `update_task_api_collab_tasks__tid__put` |
| `DELETE` | `/api/community/comments/{comment_id}` | `delete_comment_api_community_comments__comment_id__delete` |
| `POST` | `/api/community/comments/{comment_id}/like` | `like_comment_api_community_comments__comment_id__like_post` |
| `GET` | `/api/community/favorites` | `list_favorites_api_community_favorites_get` |
| `POST` | `/api/community/favorites` | `toggle_favorite_api_community_favorites_post` |
| `DELETE` | `/api/community/favorites/{object_type}/{object_id}` | `delete_favorite_api_community_favorites__object_type___object_id__delete` |
| `GET` | `/api/community/favorites/{object_type}/{object_id}` | `favorite_status_api_community_favorites__object_type___object_id__get` |
| `GET` | `/api/community/mutuals` | `list_mutuals_api_community_mutuals_get` |
| `GET` | `/api/community/posts` | `list_posts_api_community_posts_get` |
| `POST` | `/api/community/posts` | `create_post_api_community_posts_post` |
| `DELETE` | `/api/community/posts/{post_id}` | `delete_post_api_community_posts__post_id__delete` |
| `GET` | `/api/community/posts/{post_id}` | `read_post_api_community_posts__post_id__get` |
| `PUT` | `/api/community/posts/{post_id}` | `update_post_api_community_posts__post_id__put` |
| `POST` | `/api/community/posts/{post_id}/accept/{comment_id}` | `accept_answer_api_community_posts__post_id__accept__comment_id__post` |
| `POST` | `/api/community/posts/{post_id}/ai-review` | `ai_review_api_community_posts__post_id__ai_review_post` |
| `POST` | `/api/community/posts/{post_id}/clone` | `clone_from_post_api_community_posts__post_id__clone_post` |
| `POST` | `/api/community/posts/{post_id}/comments` | `add_comment_api_community_posts__post_id__comments_post` |
| `POST` | `/api/community/posts/{post_id}/dislike` | `dislike_post_api_community_posts__post_id__dislike_post` |
| `POST` | `/api/community/posts/{post_id}/favorite` | `favorite_post_api_community_posts__post_id__favorite_post` |
| `POST` | `/api/community/posts/{post_id}/feature` | `feature_post_api_community_posts__post_id__feature_post` |
| `POST` | `/api/community/posts/{post_id}/like` | `like_post_api_community_posts__post_id__like_post` |
| `POST` | `/api/community/posts/{post_id}/pin` | `pin_post_api_community_posts__post_id__pin_post` |
| `POST` | `/api/community/posts/{post_id}/qmoji` | `qmoji_post_api_community_posts__post_id__qmoji_post` |
| `POST` | `/api/community/posts/{post_id}/report` | `report_post_api_community_posts__post_id__report_post` |
| `GET` | `/api/community/profile` | `get_profile_api_community_profile_get` |
| `PUT` | `/api/community/profile` | `put_profile_api_community_profile_put` |
| `GET` | `/api/community/reports` | `list_reports_api_community_reports_get` |
| `POST` | `/api/community/reports/{report_id}/decide` | `decide_report_api_community_reports__report_id__decide_post` |
| `GET` | `/api/community/stats` | `stats_api_community_stats_get` |
| `GET` | `/api/community/tags` | `tags_api_community_tags_get` |
| `GET` | `/api/community/users/{identifier}` | `user_profile_api_community_users__identifier__get` |
| `DELETE` | `/api/community/users/{identifier}/block` | `unblock_user_api_community_users__identifier__block_delete` |
| `POST` | `/api/community/users/{identifier}/block` | `block_user_api_community_users__identifier__block_post` |
| `DELETE` | `/api/community/users/{identifier}/follow` | `unfollow_user_api_community_users__identifier__follow_delete` |
| `POST` | `/api/community/users/{identifier}/follow` | `follow_user_api_community_users__identifier__follow_post` |
| `GET` | `/api/community/users/{identifier}/showcase` | `user_showcase_api_community_users__identifier__showcase_get` |
| `GET` | `/api/comparisons` | `list_comparisons_api_comparisons_get` |
| `POST` | `/api/comparisons` | `create_comparison_api_comparisons_post` |
| `GET` | `/api/comparisons/{cid}` | `get_comparison_api_comparisons__cid__get` |
| `GET` | `/api/config` | `read_config_api_config_get` |
| `GET` | `/api/config/defaults` | `read_defaults_api_config_defaults_get` |
| `GET` | `/api/config/member-defaults` | `get_member_defaults_api_config_member_defaults_get` |
| `PUT` | `/api/config/member-defaults` | `put_member_defaults_api_config_member_defaults_put` |
| `GET` | `/api/config/members` | `member_policies_api_config_members_get` |
| `PUT` | `/api/config/members/{user_id}` | `set_member_policy_api_config_members__user_id__put` |
| `GET` | `/api/config/models` | `list_models_api_config_models_get` |
| `GET` | `/api/config/presets` | `list_presets_api_config_presets_get` |
| `GET` | `/api/config/provider-quotas` | `provider_quotas_api_config_provider_quotas_get` |
| `POST` | `/api/config/providers` | `upsert_provider_api_config_providers_post` |
| `POST` | `/api/config/providers/discover` | `discover_models_api_config_providers_discover_post` |
| `DELETE` | `/api/config/providers/{name}` | `delete_provider_api_config_providers__name__delete` |
| `GET` | `/api/config/quota-requests` | `list_quota_requests_api_config_quota_requests_get` |
| `POST` | `/api/config/quota-requests` | `create_quota_request_api_config_quota_requests_post` |
| `POST` | `/api/config/quota-requests/{rid}/decide` | `decide_quota_request_api_config_quota_requests__rid__decide_post` |
| `PUT` | `/api/config/routing` | `update_routing_api_config_routing_put` |
| `GET` | `/api/config/search` | `read_search_api_config_search_get` |
| `PUT` | `/api/config/search` | `update_search_api_config_search_put` |
| `POST` | `/api/config/search/sources` | `add_search_source_api_config_search_sources_post` |
| `DELETE` | `/api/config/search/sources/{source_id}` | `delete_search_source_api_config_search_sources__source_id__delete` |
| `GET` | `/api/config/status` | `llm_status_api_config_status_get` |
| `POST` | `/api/config/test` | `test_provider_api_config_test_post` |
| `GET` | `/api/config/usage` | `usage_api_config_usage_get` |
| `POST` | `/api/copy/exit-plan` | `copy_exit_plan_api_copy_exit_plan_post` |
| `POST` | `/api/copy/plan` | `copy_plan_api_copy_plan_post` |
| `GET` | `/api/copy/policy` | `copy_policy_options_api_copy_policy_get` |
| `GET` | `/api/copy/report` | `copy_report_api_copy_report_get` |
| `GET` | `/api/datahub/auto` | `auto_status_api_datahub_auto_get` |
| `POST` | `/api/datahub/auto` | `auto_set_api_datahub_auto_post` |
| `POST` | `/api/datahub/auto/run` | `auto_run_api_datahub_auto_run_post` |
| `GET` | `/api/datahub/connectors` | `list_connectors_api_datahub_connectors_get` |
| `GET` | `/api/datahub/datasets` | `list_datasets_api_datahub_datasets_get` |
| `DELETE` | `/api/datahub/datasets/{key}` | `delete_dataset_api_datahub_datasets__key__delete` |
| `GET` | `/api/datahub/datasets/{key}/series` | `dataset_series_api_datahub_datasets__key__series_get` |
| `GET` | `/api/datahub/events` | `list_events_api_datahub_events_get` |
| `POST` | `/api/datahub/import` | `import_csv_api_datahub_import_post` |
| `POST` | `/api/datahub/keys` | `set_key_api_datahub_keys_post` |
| `GET` | `/api/datahub/liquidations` | `liquidations_status_api_datahub_liquidations_get` |
| `POST` | `/api/datahub/liquidations` | `liquidations_control_api_datahub_liquidations_post` |
| `POST` | `/api/datahub/sync` | `sync_api_datahub_sync_post` |
| `GET` | `/api/demo/connections` | `list_connections_api_demo_connections_get` |
| `POST` | `/api/demo/connections` | `connect_api_demo_connections_post` |
| `DELETE` | `/api/demo/connections/{cid}` | `revoke_connection_api_demo_connections__cid__delete` |
| `GET` | `/api/demo/connections/{cid}/contract-bills` | `contract_bills_api_demo_connections__cid__contract_bills_get` |
| `POST` | `/api/demo/connections/{cid}/test` | `test_connection_api_demo_connections__cid__test_post` |
| `GET` | `/api/demo/environments` | `environments_api_demo_environments_get` |
| `GET` | `/api/demo/live` | `live_quote_api_demo_live_get` |
| `POST` | `/api/dsl/from-code` | `dsl_from_code_api_dsl_from_code_post` |
| `POST` | `/api/dsl/rank` | `dsl_rank_api_dsl_rank_post` |
| `GET` | `/api/dsl/schema` | `dsl_schema_api_dsl_schema_get` |
| `POST` | `/api/dsl/strategies` | `save_dsl_strategy_api_dsl_strategies_post` |
| `POST` | `/api/dsl/strategies/{slug}/to-code` | `convert_dsl_to_code_api_dsl_strategies__slug__to_code_post` |
| `POST` | `/api/dsl/to-code` | `dsl_to_code_api_dsl_to_code_post` |
| `POST` | `/api/dsl/validate` | `dsl_validate_api_dsl_validate_post` |
| `GET` | `/api/envelopes/schema` | `envelope_schema_api_envelopes_schema_get` |
| `POST` | `/api/envelopes/validate` | `validate_envelopes_api_envelopes_validate_post` |
| `GET` | `/api/execution` | `schemes_api_execution_get` |
| `POST` | `/api/execution` | `save_api_execution_post` |
| `POST` | `/api/execution/capacity` | `capacity_api_execution_capacity_post` |
| `GET` | `/api/execution/costs` | `costs_api_execution_costs_get` |
| `POST` | `/api/execution/costs/calibrate` | `calibrate_api_execution_costs_calibrate_post` |
| `DELETE` | `/api/execution/costs/{cid}` | `cost_delete_api_execution_costs__cid__delete` |
| `GET` | `/api/execution/costs/{cid}` | `cost_detail_api_execution_costs__cid__get` |
| `POST` | `/api/execution/costs/{cid}/activate` | `cost_activate_api_execution_costs__cid__activate_post` |
| `POST` | `/api/execution/evaluate` | `evaluate_api_execution_evaluate_post` |
| `GET` | `/api/execution/meta` | `meta_api_execution_meta_get` |
| `GET` | `/api/execution/radar` | `radar_api_execution_radar_get` |
| `GET` | `/api/execution/recordings` | `recordings_api_execution_recordings_get` |
| `POST` | `/api/execution/recordings` | `record_api_execution_recordings_post` |
| `DELETE` | `/api/execution/recordings/{rid}` | `recording_delete_api_execution_recordings__rid__delete` |
| `GET` | `/api/execution/recordings/{rid}` | `recording_api_execution_recordings__rid__get` |
| `POST` | `/api/execution/recordings/{rid}/stop` | `recording_stop_api_execution_recordings__rid__stop_post` |
| `POST` | `/api/execution/replay` | `replay_api_execution_replay_post` |
| `GET` | `/api/execution/runs` | `runs_api_execution_runs_get` |
| `DELETE` | `/api/execution/runs/{rid}` | `run_delete_api_execution_runs__rid__delete` |
| `GET` | `/api/execution/runs/{rid}` | `run_detail_api_execution_runs__rid__get` |
| `DELETE` | `/api/execution/{sid}` | `scheme_delete_api_execution__sid__delete` |
| `GET` | `/api/execution/{sid}` | `scheme_api_execution__sid__get` |
| `PATCH` | `/api/execution/{sid}` | `scheme_patch_api_execution__sid__patch` |
| `GET` | `/api/execution/{sid}/report` | `scheme_report_api_execution__sid__report_get` |
| `GET` | `/api/factors` | `list_factors_api_factors_get` |
| `POST` | `/api/factors` | `create_factor_api_factors_post` |
| `POST` | `/api/factors/evaluate` | `evaluate_expression_api_factors_evaluate_post` |
| `GET` | `/api/factors/meta` | `factors_meta_api_factors_meta_get` |
| `GET` | `/api/factors/trials` | `list_trials_api_factors_trials_get` |
| `POST` | `/api/factors/validate` | `validate_expression_api_factors_validate_post` |
| `DELETE` | `/api/factors/{fid}` | `delete_factor_api_factors__fid__delete` |
| `GET` | `/api/factors/{fid}` | `get_factor_api_factors__fid__get` |
| `PATCH` | `/api/factors/{fid}` | `patch_factor_api_factors__fid__patch` |
| `POST` | `/api/factors/{fid}/refresh` | `refresh_factor_api_factors__fid__refresh_post` |
| `GET` | `/api/factors/{fid}/report` | `factor_report_api_factors__fid__report_get` |
| `POST` | `/api/factors/{fid}/status` | `set_status_api_factors__fid__status_post` |
| `POST` | `/api/freqtrade/check` | `check_api_freqtrade_check_post` |
| `GET` | `/api/freqtrade/export/{slug}` | `export_strategy_api_freqtrade_export__slug__get` |
| `POST` | `/api/freqtrade/import` | `import_strategy_api_freqtrade_import_post` |
| `POST` | `/api/freqtrade/rewrite` | `rewrite_api_freqtrade_rewrite_post` |
| `GET` | `/api/freqtrade/support` | `support_api_freqtrade_support_get` |
| `GET` | `/api/guidances` | `guidances_api_guidances_get` |
| `POST` | `/api/guidances/render` | `render_guidance_api_guidances_render_post` |
| `GET` | `/api/health` | `health_api_health_get` |
| `GET` | `/api/healthz` | `healthz_api_healthz_get` |
| `GET` | `/api/imports` | `list_imports_api_imports_get` |
| `POST` | `/api/imports` | `create_import_api_imports_post` |
| `GET` | `/api/imports/spec` | `import_spec_api_imports_spec_get` |
| `DELETE` | `/api/imports/{import_id}` | `delete_import_api_imports__import_id__delete` |
| `GET` | `/api/imports/{import_id}` | `get_import_api_imports__import_id__get` |
| `GET` | `/api/imports/{import_id}/export.csv` | `export_import_api_imports__import_id__export_csv_get` |
| `GET` | `/api/inbox` | `inbox_api_inbox_get` |
| `POST` | `/api/inbox/clear` | `inbox_clear_api_inbox_clear_post` |
| `GET` | `/api/inbox/mutes` | `list_mutes_api_inbox_mutes_get` |
| `POST` | `/api/inbox/mutes` | `add_mute_api_inbox_mutes_post` |
| `DELETE` | `/api/inbox/mutes/{target_type}/{target_id}` | `remove_mute_api_inbox_mutes__target_type___target_id__delete` |
| `POST` | `/api/inbox/read` | `inbox_read_api_inbox_read_post` |
| `GET` | `/api/inbox/summary` | `inbox_summary_api_inbox_summary_get` |
| `GET` | `/api/issues` | `list_issues_api_issues_get` |
| `POST` | `/api/issues` | `create_issue_api_issues_post` |
| `GET` | `/api/issues/{iid}` | `get_issue_api_issues__iid__get` |
| `POST` | `/api/issues/{iid}/replies` | `reply_issue_api_issues__iid__replies_post` |
| `PUT` | `/api/issues/{iid}/status` | `issue_status_api_issues__iid__status_put` |
| `POST` | `/api/lab/goal/check` | `check_goal_api_lab_goal_check_post` |
| `POST` | `/api/lab/goal/parse` | `parse_goal_api_lab_goal_parse_post` |
| `GET` | `/api/lab/meta` | `lab_meta_api_lab_meta_get` |
| `GET` | `/api/lab/projects` | `list_projects_api_lab_projects_get` |
| `POST` | `/api/lab/projects` | `create_project_api_lab_projects_post` |
| `DELETE` | `/api/lab/projects/{pid}` | `delete_project_api_lab_projects__pid__delete` |
| `GET` | `/api/lab/projects/{pid}` | `get_project_api_lab_projects__pid__get` |
| `PATCH` | `/api/lab/projects/{pid}` | `patch_project_api_lab_projects__pid__patch` |
| `POST` | `/api/lab/projects/{pid}/autopilot` | `start_autopilot_api_lab_projects__pid__autopilot_post` |
| `POST` | `/api/lab/projects/{pid}/backtest` | `backtest_project_api_lab_projects__pid__backtest_post` |
| `POST` | `/api/lab/projects/{pid}/code` | `save_code_api_lab_projects__pid__code_post` |
| `POST` | `/api/lab/projects/{pid}/compact` | `compact_history_api_lab_projects__pid__compact_post` |
| `POST` | `/api/lab/projects/{pid}/cost-sweep` | `cost_sweep_project_api_lab_projects__pid__cost_sweep_post` |
| `POST` | `/api/lab/projects/{pid}/decision` | `decide_api_lab_projects__pid__decision_post` |
| `POST` | `/api/lab/projects/{pid}/discussion` | `attach_discussion_api_lab_projects__pid__discussion_post` |
| `GET` | `/api/lab/projects/{pid}/events` | `project_events_api_lab_projects__pid__events_get` |
| `POST` | `/api/lab/projects/{pid}/fork` | `fork_project_api_lab_projects__pid__fork_post` |
| `POST` | `/api/lab/projects/{pid}/goal` | `set_goal_api_lab_projects__pid__goal_post` |
| `GET` | `/api/lab/projects/{pid}/images/{iid}` | `get_image_api_lab_projects__pid__images__iid__get` |
| `POST` | `/api/lab/projects/{pid}/messages` | `send_message_api_lab_projects__pid__messages_post` |
| `POST` | `/api/lab/projects/{pid}/messages/{seq}/feedback` | `message_feedback_api_lab_projects__pid__messages__seq__feedback_post` |
| `POST` | `/api/lab/projects/{pid}/parameter-heatmap` | `parameter_heatmap_project_api_lab_projects__pid__parameter_heatmap_post` |
| `POST` | `/api/lab/projects/{pid}/pins` | `add_pin_api_lab_projects__pid__pins_post` |
| `DELETE` | `/api/lab/projects/{pid}/pins/{pin_id}` | `remove_pin_api_lab_projects__pid__pins__pin_id__delete` |
| `POST` | `/api/lab/projects/{pid}/plan` | `run_plan_api_lab_projects__pid__plan_post` |
| `POST` | `/api/lab/projects/{pid}/render-errors` | `report_render_errors_api_lab_projects__pid__render_errors_post` |
| `POST` | `/api/lab/projects/{pid}/report` | `run_report_api_lab_projects__pid__report_post` |
| `GET` | `/api/lab/projects/{pid}/reports/{rid}` | `get_report_api_lab_projects__pid__reports__rid__get` |
| `POST` | `/api/lab/projects/{pid}/rewind` | `rewind_project_api_lab_projects__pid__rewind_post` |
| `POST` | `/api/lab/projects/{pid}/save` | `save_as_strategy_api_lab_projects__pid__save_post` |
| `POST` | `/api/lab/projects/{pid}/stop` | `stop_project_api_lab_projects__pid__stop_post` |
| `POST` | `/api/lab/projects/{pid}/stress-test` | `stress_test_project_api_lab_projects__pid__stress_test_post` |
| `POST` | `/api/lab/projects/{pid}/versions/{v}/restore` | `restore_version_api_lab_projects__pid__versions__v__restore_post` |
| `POST` | `/api/lab/projects/{pid}/walkforward` | `walk_forward_api_lab_projects__pid__walkforward_post` |
| `GET` | `/api/lifecycle` | `overview_api_lifecycle_get` |
| `POST` | `/api/lifecycle/check` | `check_now_api_lifecycle_check_post` |
| `POST` | `/api/lifecycle/{session_id}/demote` | `demote_api_lifecycle__session_id__demote_post` |
| `GET` | `/api/live/status` | `live_status_api_live_status_get` |
| `POST` | `/api/long-running/digest/run` | `run_digest_api_long_running_digest_run_post` |
| `POST` | `/api/long-running/maintainer/run` | `run_maintainer_api_long_running_maintainer_run_post` |
| `GET` | `/api/long-running/preview` | `preview_digest_api_long_running_preview_get` |
| `GET` | `/api/market/banner` | `status_banner_api_market_banner_get` |
| `GET` | `/api/market/bars` | `bars_api_market_bars_get` |
| `DELETE` | `/api/market/cache` | `clear_cache_api_market_cache_delete` |
| `GET` | `/api/market/cache` | `cache_entries_api_market_cache_get` |
| `POST` | `/api/market/cache/fetch` | `prefetch_api_market_cache_fetch_post` |
| `DELETE` | `/api/market/db-cache` | `db_cache_clear_api_market_db_cache_delete` |
| `GET` | `/api/market/db-cache` | `db_cache_stats_api_market_db_cache_get` |
| `GET` | `/api/market/network` | `network_status_api_market_network_get` |
| `POST` | `/api/market/network/probe` | `network_probe_api_market_network_probe_post` |
| `GET` | `/api/market/quality` | `quality_api_market_quality_get` |
| `GET` | `/api/market/quotes` | `quotes_api_market_quotes_get` |
| `GET` | `/api/market/summary` | `summary_api_market_summary_get` |
| `GET` | `/api/market/symbols` | `symbols_api_market_symbols_get` |
| `POST` | `/api/market/sync` | `sync_data_api_market_sync_post` |
| `GET` | `/api/market/ticker` | `ticker_api_market_ticker_get` |
| `GET` | `/api/media/image` | `image_api_media_image_get` |
| `GET` | `/api/mentor/course` | `course_api_mentor_course_get` |
| `PUT` | `/api/mentor/progress` | `progress_api_mentor_progress_put` |
| `GET` | `/api/microlive` | `overview_api_microlive_get` |
| `POST` | `/api/microlive/connections` | `connect_api_microlive_connections_post` |
| `DELETE` | `/api/microlive/connections/{cid}` | `revoke_api_microlive_connections__cid__delete` |
| `POST` | `/api/microlive/connections/{cid}/test` | `test_connection_api_microlive_connections__cid__test_post` |
| `POST` | `/api/microlive/disable` | `disable_api_microlive_disable_post` |
| `POST` | `/api/microlive/enable` | `enable_api_microlive_enable_post` |
| `POST` | `/api/microlive/kill` | `kill_api_microlive_kill_post` |
| `GET` | `/api/microlive/notice` | `notice_api_microlive_notice_get` |
| `POST` | `/api/microlive/prepare` | `prepare_api_microlive_prepare_post` |
| `POST` | `/api/microlive/rearm` | `rearm_api_microlive_rearm_post` |
| `POST` | `/api/microlive/start` | `start_api_microlive_start_post` |
| `POST` | `/api/microlive/tiers/{slug}/lower` | `lower_tier_api_microlive_tiers__slug__lower_post` |
| `POST` | `/api/microlive/tiers/{slug}/raise` | `raise_tier_api_microlive_tiers__slug__raise_post` |
| `GET` | `/api/missions` | `list_missions_api_missions_get` |
| `POST` | `/api/missions` | `create_mission_api_missions_post` |
| `POST` | `/api/missions/pipelines` | `save_pipeline_api_missions_pipelines_post` |
| `DELETE` | `/api/missions/pipelines/{pid}` | `delete_pipeline_api_missions_pipelines__pid__delete` |
| `GET` | `/api/missions/presets` | `presets_api_missions_presets_get` |
| `DELETE` | `/api/missions/{mid}` | `delete_mission_api_missions__mid__delete` |
| `GET` | `/api/missions/{mid}` | `get_mission_api_missions__mid__get` |
| `POST` | `/api/missions/{mid}/approve` | `approve_api_missions__mid__approve_post` |
| `POST` | `/api/missions/{mid}/say` | `say_api_missions__mid__say_post` |
| `POST` | `/api/missions/{mid}/stop` | `stop_api_missions__mid__stop_post` |
| `GET` | `/api/models` | `list_models_api_models_get` |
| `POST` | `/api/models` | `save_model_api_models_post` |
| `GET` | `/api/models/compare` | `compare_runs_api_models_compare_get` |
| `GET` | `/api/models/jobs/{jid}` | `job_status_api_models_jobs__jid__get` |
| `POST` | `/api/models/jobs/{jid}/cancel` | `job_cancel_api_models_jobs__jid__cancel_post` |
| `GET` | `/api/models/meta` | `models_meta_api_models_meta_get` |
| `GET` | `/api/models/runs` | `list_runs_api_models_runs_get` |
| `DELETE` | `/api/models/runs/{rid}` | `delete_run_api_models_runs__rid__delete` |
| `GET` | `/api/models/runs/{rid}` | `get_run_api_models_runs__rid__get` |
| `POST` | `/api/models/train` | `train_api_models_train_post` |
| `DELETE` | `/api/models/{mid}` | `delete_model_api_models__mid__delete` |
| `GET` | `/api/models/{mid}` | `get_model_api_models__mid__get` |
| `PATCH` | `/api/models/{mid}` | `patch_model_api_models__mid__patch` |
| `POST` | `/api/models/{mid}/refresh` | `refresh_model_api_models__mid__refresh_post` |
| `GET` | `/api/models/{mid}/report` | `model_report_api_models__mid__report_get` |
| `POST` | `/api/models/{mid}/retrain` | `retrain_now_api_models__mid__retrain_post` |
| `POST` | `/api/models/{mid}/retrain-plan` | `retrain_plan_api_models__mid__retrain_plan_post` |
| `GET` | `/api/node/board` | `node_board_api_node_board_get` |
| `GET` | `/api/node/health` | `node_health_api_node_health_get` |
| `GET` | `/api/notifications` | `list_notifications_api_notifications_get` |
| `POST` | `/api/notifications/flush` | `flush_notifications_api_notifications_flush_post` |
| `POST` | `/api/notifications/test` | `test_notification_api_notifications_test_post` |
| `GET` | `/api/notify/channels` | `list_notify_channels_api_notify_channels_get` |
| `POST` | `/api/notify/channels` | `create_notify_channel_api_notify_channels_post` |
| `DELETE` | `/api/notify/channels/{channel_id}` | `delete_notify_channel_api_notify_channels__channel_id__delete` |
| `GET` | `/api/overview` | `overview_api_overview_get` |
| `GET` | `/api/portfolio` | `list_schemes_api_portfolio_get` |
| `POST` | `/api/portfolio` | `save_scheme_api_portfolio_post` |
| `POST` | `/api/portfolio/evaluate` | `evaluate_api_portfolio_evaluate_post` |
| `GET` | `/api/portfolio/meta` | `portfolio_meta_api_portfolio_meta_get` |
| `POST` | `/api/portfolio/risk` | `risk_api_portfolio_risk_post` |
| `GET` | `/api/portfolio/runs` | `list_runs_api_portfolio_runs_get` |
| `DELETE` | `/api/portfolio/runs/{rid}` | `delete_run_api_portfolio_runs__rid__delete` |
| `GET` | `/api/portfolio/runs/{rid}` | `get_run_api_portfolio_runs__rid__get` |
| `DELETE` | `/api/portfolio/{sid}` | `delete_scheme_api_portfolio__sid__delete` |
| `GET` | `/api/portfolio/{sid}` | `get_scheme_api_portfolio__sid__get` |
| `PATCH` | `/api/portfolio/{sid}` | `patch_scheme_api_portfolio__sid__patch` |
| `GET` | `/api/portfolio/{sid}/report` | `scheme_report_api_portfolio__sid__report_get` |
| `GET` | `/api/public/shares/{token}` | `public_share_api_public_shares__token__get` |
| `POST` | `/api/rd/assemble` | `assemble_api_rd_assemble_post` |
| `GET` | `/api/rd/assemble/options` | `assemble_options_api_rd_assemble_options_get` |
| `GET` | `/api/rd/brief` | `latest_brief_api_rd_brief_get` |
| `POST` | `/api/rd/brief` | `make_brief_api_rd_brief_post` |
| `GET` | `/api/rd/brief/{bid}` | `get_brief_api_rd_brief__bid__get` |
| `GET` | `/api/rd/holdout` | `holdout_api_rd_holdout_get` |
| `POST` | `/api/rd/holdout` | `set_holdout_api_rd_holdout_post` |
| `POST` | `/api/rd/holdout/remove` | `remove_holdout_api_rd_holdout_remove_post` |
| `POST` | `/api/rd/holdout/unseal` | `unseal_api_rd_holdout_unseal_post` |
| `GET` | `/api/rd/ledger` | `ledger_api_rd_ledger_get` |
| `POST` | `/api/rd/ledger/pbo` | `pbo_api_rd_ledger_pbo_post` |
| `GET` | `/api/rd/lineage` | `lineage_api_rd_lineage_get` |
| `GET` | `/api/rd/nightshift` | `nightshift_api_rd_nightshift_get` |
| `PUT` | `/api/rd/nightshift/config` | `nightshift_config_api_rd_nightshift_config_put` |
| `POST` | `/api/rd/nightshift/run` | `nightshift_run_api_rd_nightshift_run_post` |
| `GET` | `/api/rd/nightshift/shifts/{sid}` | `nightshift_detail_api_rd_nightshift_shifts__sid__get` |
| `POST` | `/api/rd/nightshift/stop` | `nightshift_stop_api_rd_nightshift_stop_post` |
| `POST` | `/api/rd/nightshift/tasks` | `add_night_task_api_rd_nightshift_tasks_post` |
| `DELETE` | `/api/rd/nightshift/tasks/{tid}` | `remove_night_task_api_rd_nightshift_tasks__tid__delete` |
| `GET` | `/api/rd/pipeline` | `pipeline_api_rd_pipeline_get` |
| `GET` | `/api/rd/shadows` | `shadows_api_rd_shadows_get` |
| `POST` | `/api/rd/shadows` | `create_shadow_api_rd_shadows_post` |
| `GET` | `/api/rd/shadows/{sid}` | `shadow_detail_api_rd_shadows__sid__get` |
| `POST` | `/api/rd/shadows/{sid}/end` | `end_shadow_api_rd_shadows__sid__end_post` |
| `POST` | `/api/rd/shadows/{sid}/promote` | `promote_shadow_api_rd_shadows__sid__promote_post` |
| `GET` | `/api/realperp` | `overview_api_realperp_get` |
| `POST` | `/api/realperp/connections` | `connect_api_realperp_connections_post` |
| `DELETE` | `/api/realperp/connections/{cid}` | `revoke_api_realperp_connections__cid__delete` |
| `POST` | `/api/realperp/connections/{cid}/test` | `test_connection_api_realperp_connections__cid__test_post` |
| `POST` | `/api/realperp/disable` | `disable_api_realperp_disable_post` |
| `POST` | `/api/realperp/enable` | `enable_api_realperp_enable_post` |
| `POST` | `/api/realperp/kill` | `kill_api_realperp_kill_post` |
| `GET` | `/api/realperp/notice` | `notice_api_realperp_notice_get` |
| `POST` | `/api/realperp/prepare` | `prepare_api_realperp_prepare_post` |
| `POST` | `/api/realperp/rearm` | `rearm_api_realperp_rearm_post` |
| `POST` | `/api/realperp/start` | `start_api_realperp_start_post` |
| `GET` | `/api/research/backtest-retention` | `get_backtest_retention_api_research_backtest_retention_get` |
| `PUT` | `/api/research/backtest-retention` | `put_backtest_retention_api_research_backtest_retention_put` |
| `POST` | `/api/research/backtest-retention/cleanup` | `cleanup_backtest_retention_api_research_backtest_retention_cleanup_post` |
| `PUT` | `/api/research/backtest-retention/workspace` | `put_workspace_backtest_retention_api_research_backtest_retention_workspace_put` |
| `GET` | `/api/research/backtests` | `list_backtests_api_research_backtests_get` |
| `POST` | `/api/research/backtests` | `run_backtest_endpoint_api_research_backtests_post` |
| `POST` | `/api/research/backtests/batch-delete` | `batch_delete_backtests_api_research_backtests_batch_delete_post` |
| `POST` | `/api/research/backtests/compare` | `compare_backtests_api_research_backtests_compare_post` |
| `POST` | `/api/research/backtests/package/inspect` | `inspect_research_package_api_research_backtests_package_inspect_post` |
| `POST` | `/api/research/backtests/package/replay` | `replay_research_package_api_research_backtests_package_replay_post` |
| `DELETE` | `/api/research/backtests/{bt_id}` | `delete_backtest_api_research_backtests__bt_id__delete` |
| `GET` | `/api/research/backtests/{bt_id}` | `get_backtest_api_research_backtests__bt_id__get` |
| `GET` | `/api/research/backtests/{bt_id}/package` | `download_research_package_api_research_backtests__bt_id__package_get` |
| `POST` | `/api/research/backtests/{bt_id}/pin` | `pin_backtest_api_research_backtests__bt_id__pin_post` |
| `GET` | `/api/research/compare-batches` | `list_compare_batches_api_research_compare_batches_get` |
| `POST` | `/api/research/compare-batches` | `create_compare_batch_api_research_compare_batches_post` |
| `DELETE` | `/api/research/compare-batches/{batch_id}` | `delete_compare_batch_api_research_compare_batches__batch_id__delete` |
| `GET` | `/api/research/compare-batches/{batch_id}` | `get_compare_batch_api_research_compare_batches__batch_id__get` |
| `POST` | `/api/research/compare-batches/{batch_id}/results` | `add_compare_result_api_research_compare_batches__batch_id__results_post` |
| `GET` | `/api/research/jobs/{job_id}` | `job_status_api_research_jobs__job_id__get` |
| `GET` | `/api/research/optimizations` | `list_optimizations_api_research_optimizations_get` |
| `GET` | `/api/research/optimizations/{opt_id}` | `get_optimization_api_research_optimizations__opt_id__get` |
| `POST` | `/api/research/optimize` | `start_optimize_api_research_optimize_post` |
| `POST` | `/api/research/pycode` | `to_python_api_research_pycode_post` |
| `GET` | `/api/research/record-retention` | `get_record_retention_api_research_record_retention_get` |
| `PUT` | `/api/research/record-retention` | `put_record_retention_api_research_record_retention_put` |
| `POST` | `/api/research/record-retention/cleanup` | `cleanup_record_retention_api_research_record_retention_cleanup_post` |
| `PUT` | `/api/research/record-retention/multipliers` | `put_record_multipliers_api_research_record_retention_multipliers_put` |
| `GET` | `/api/research/record-retention/records` | `list_retention_records_api_research_record_retention_records_get` |
| `PUT` | `/api/research/record-retention/workspace` | `put_record_retention_workspace_api_research_record_retention_workspace_put` |
| `DELETE` | `/api/research/record-retention/{type}/{record_id}` | `delete_retention_record_api_research_record_retention__type___record_id__delete` |
| `POST` | `/api/research/record-retention/{type}/{record_id}/pin` | `pin_retention_record_api_research_record_retention__type___record_id__pin_post` |
| `POST` | `/api/research/strategies` | `new_strategy_api_research_strategies_post` |
| `POST` | `/api/research/strategies/{slug}/apply-params` | `apply_params_api_research_strategies__slug__apply_params_post` |
| `POST` | `/api/research/strategies/{slug}/clone` | `clone_strategy_api_research_strategies__slug__clone_post` |
| `GET` | `/api/research/strategies/{slug}/params` | `strategy_params_api_research_strategies__slug__params_get` |
| `GET` | `/api/research/templates` | `templates_api_research_templates_get` |
| `GET` | `/api/research/templates/{template_id}` | `template_detail_api_research_templates__template_id__get` |
| `GET` | `/api/runs` | `list_runs_api_runs_get` |
| `GET` | `/api/runs/workers` | `workers_api_runs_workers_get` |
| `GET` | `/api/runs/{run_id}` | `get_run_api_runs__run_id__get` |
| `POST` | `/api/runs/{run_id}/commands` | `send_command_api_runs__run_id__commands_post` |
| `GET` | `/api/runs/{run_id}/envelopes` | `run_envelopes_api_runs__run_id__envelopes_get` |
| `GET` | `/api/runs/{run_id}/events` | `run_events_api_runs__run_id__events_get` |
| `GET` | `/api/runs/{run_id}/fills` | `run_fills_api_runs__run_id__fills_get` |
| `GET` | `/api/runs/{run_id}/nav` | `run_nav_api_runs__run_id__nav_get` |
| `GET` | `/api/runs/{run_id}/result` | `run_result_api_runs__run_id__result_get` |
| `GET` | `/api/runs/{run_id}/series` | `run_series_api_runs__run_id__series_get` |
| `GET` | `/api/runs/{run_id}/signals` | `run_signals_api_runs__run_id__signals_get` |
| `GET` | `/api/schedules` | `list_schedules_api_schedules_get` |
| `POST` | `/api/schedules` | `create_schedule_api_schedules_post` |
| `POST` | `/api/schedules/tick` | `tick_schedules_api_schedules_tick_post` |
| `DELETE` | `/api/schedules/{job_id}` | `delete_schedule_api_schedules__job_id__delete` |
| `GET` | `/api/strategies` | `list_strategies_api_strategies_get` |
| `POST` | `/api/strategies` | `create_strategy_api_strategies_post` |
| `GET` | `/api/strategies-migration` | `migration_status_api_strategies_migration_get` |
| `POST` | `/api/strategies-migration` | `migrate_strategies_api_strategies_migration_post` |
| `POST` | `/api/strategies/generate` | `generate_strategy_api_strategies_generate_post` |
| `GET` | `/api/strategies/{slug}/versions` | `list_versions_api_strategies__slug__versions_get` |
| `DELETE` | `/api/strategies/{strategy_id}` | `delete_strategy_api_strategies__strategy_id__delete` |
| `GET` | `/api/strategies/{strategy_id}` | `get_strategy_api_strategies__strategy_id__get` |
| `POST` | `/api/strategies/{strategy_id}/approve` | `approve_strategy_api_strategies__strategy_id__approve_post` |
| `GET` | `/api/strategy-versions/{version_id}` | `get_version_api_strategy_versions__version_id__get` |
| `POST` | `/api/strategy-versions/{version_id}/archive` | `archive_version_api_strategy_versions__version_id__archive_post` |
| `POST` | `/api/strategy-versions/{version_id}/freeze` | `freeze_version_api_strategy_versions__version_id__freeze_post` |
| `POST` | `/api/studio/backtest` | `studio_backtest_api_studio_backtest_post` |
| `GET` | `/api/studio/exchanges/capabilities` | `exchange_capabilities_api_studio_exchanges_capabilities_get` |
| `POST` | `/api/studio/order` | `submit_order_api_studio_order_post` |
| `POST` | `/api/studio/packages/import` | `import_package_api_studio_packages_import_post` |
| `POST` | `/api/studio/packages/inspect` | `inspect_package_api_studio_packages_inspect_post` |
| `GET` | `/api/studio/paper` | `list_paper_sessions_api_studio_paper_get` |
| `POST` | `/api/studio/paper/start` | `start_paper_api_studio_paper_start_post` |
| `DELETE` | `/api/studio/paper/{session_id}` | `delete_paper_api_studio_paper__session_id__delete` |
| `POST` | `/api/studio/paper/{session_id}/liquidate` | `liquidate_paper_api_studio_paper__session_id__liquidate_post` |
| `POST` | `/api/studio/paper/{session_id}/pause` | `pause_paper_api_studio_paper__session_id__pause_post` |
| `POST` | `/api/studio/paper/{session_id}/recover` | `recover_paper_api_studio_paper__session_id__recover_post` |
| `GET` | `/api/studio/paper/{session_id}/report` | `paper_report_api_studio_paper__session_id__report_get` |
| `POST` | `/api/studio/paper/{session_id}/resume` | `resume_paper_api_studio_paper__session_id__resume_post` |
| `POST` | `/api/studio/paper/{session_id}/stop` | `stop_paper_api_studio_paper__session_id__stop_post` |
| `GET` | `/api/studio/paper/{session_id}/terminal` | `paper_terminal_api_studio_paper__session_id__terminal_get` |
| `POST` | `/api/studio/paper/{session_id}/tick` | `paper_tick_api_studio_paper__session_id__tick_post` |
| `POST` | `/api/studio/paper/{session_id}/unhalt` | `paper_unhalt_api_studio_paper__session_id__unhalt_post` |
| `POST` | `/api/studio/reconcile` | `run_reconcile_api_studio_reconcile_post` |
| `POST` | `/api/studio/rules/preview-python` | `preview_rule_python_api_studio_rules_preview_python_post` |
| `GET` | `/api/studio/strategies` | `list_strategies_api_studio_strategies_get` |
| `POST` | `/api/studio/strategies` | `create_or_update_strategy_api_studio_strategies_post` |
| `POST` | `/api/studio/strategies/from-rule` | `strategy_from_rule_api_studio_strategies_from_rule_post` |
| `DELETE` | `/api/studio/strategies/{slug}` | `delete_strategy_api_studio_strategies__slug__delete` |
| `GET` | `/api/studio/strategies/{slug}` | `get_strategy_api_studio_strategies__slug__get` |
| `POST` | `/api/studio/strategies/{slug}/approve` | `approve_strategy_api_studio_strategies__slug__approve_post` |
| `POST` | `/api/studio/strategies/{slug}/check` | `check_strategy_api_studio_strategies__slug__check_post` |
| `GET` | `/api/studio/strategies/{slug}/export` | `export_strategy_api_studio_strategies__slug__export_get` |
| `GET` | `/api/studio/strategies/{slug}/package` | `export_package_api_studio_strategies__slug__package_get` |
| `GET` | `/api/studio/strategies/{slug}/spec` | `get_spec_api_studio_strategies__slug__spec_get` |
| `PUT` | `/api/studio/strategies/{slug}/spec` | `put_spec_api_studio_strategies__slug__spec_put` |
| `GET` | `/api/studio/strategies/{slug}/versions/{filename}` | `read_version_api_studio_strategies__slug__versions__filename__get` |
| `GET` | `/api/template-runs` | `list_runs_api_template_runs_get` |
| `DELETE` | `/api/template-runs/{run_id}` | `delete_run_api_template_runs__run_id__delete` |
| `GET` | `/api/template-runs/{run_id}` | `get_run_api_template_runs__run_id__get` |
| `GET` | `/api/templates` | `templates_api_templates_get` |
| `POST` | `/api/uploads` | `upload_api_uploads_post` |
| `GET` | `/api/uploads/{fid}` | `read_upload_api_uploads__fid__get` |
| `GET` | `/api/uploads/{fid}/thumb` | `read_upload_thumb_api_uploads__fid__thumb_get` |
| `GET` | `/api/validation/calibration` | `calibrations_api_validation_calibration_get` |
| `GET` | `/api/validation/checkup` | `checkup_list_api_validation_checkup_get` |
| `POST` | `/api/validation/checkup` | `checkup_start_api_validation_checkup_post` |
| `GET` | `/api/validation/checkup/{cid}` | `checkup_get_api_validation_checkup__cid__get` |
| `GET` | `/api/validation/fidelity` | `list_fidelity_api_validation_fidelity_get` |
| `POST` | `/api/validation/fidelity` | `make_fidelity_api_validation_fidelity_post` |
| `GET` | `/api/validation/fidelity/{rid}` | `get_fidelity_api_validation_fidelity__rid__get` |
| `POST` | `/api/validation/fidelity/{rid}/calibration` | `apply_calibration_api_validation_fidelity__rid__calibration_post` |
| `GET` | `/api/validation/graduation` | `graduation_board_api_validation_graduation_get` |
| `POST` | `/api/validation/graduation-config` | `graduation_config_api_validation_graduation_config_post` |
| `GET` | `/api/validation/graduation/{slug}` | `graduation_detail_api_validation_graduation__slug__get` |
| `POST` | `/api/validation/graduation/{slug}/demote` | `graduation_demote_api_validation_graduation__slug__demote_post` |
| `POST` | `/api/validation/graduation/{slug}/promote` | `graduation_promote_api_validation_graduation__slug__promote_post` |
| `GET` | `/api/validation/stress` | `stress_list_api_validation_stress_get` |
| `POST` | `/api/validation/stress` | `stress_start_api_validation_stress_post` |
| `GET` | `/api/validation/stress/scenarios` | `stress_scenarios_api_validation_stress_scenarios_get` |
| `GET` | `/api/validation/stress/{rid}` | `stress_get_api_validation_stress__rid__get` |
| `GET` | `/api/validation/watcher` | `watcher_list_api_validation_watcher_get` |
| `GET` | `/api/validation/watcher/{session_id}` | `watcher_get_api_validation_watcher__session_id__get` |
| `POST` | `/api/validation/watcher/{session_id}` | `watcher_run_api_validation_watcher__session_id__post` |
| `GET` | `/api/workspace` | `workspace_detail_api_workspace_get` |
| `GET` | `/api/workspace/audit` | `audit_log_api_workspace_audit_get` |
| `POST` | `/api/workspace/audit/archive` | `audit_archive_api_workspace_audit_archive_post` |
| `GET` | `/api/workspace/audit/archived` | `audit_archived_api_workspace_audit_archived_get` |
| `GET` | `/api/workspace/audit/export` | `audit_export_api_workspace_audit_export_get` |
| `GET` | `/api/workspace/features` | `workspace_features_api_workspace_features_get` |
| `PUT` | `/api/workspace/features` | `update_workspace_features_api_workspace_features_put` |
| `POST` | `/api/workspace/invites` | `create_invite_api_workspace_invites_post` |
| `DELETE` | `/api/workspace/invites/{invite_id}` | `revoke_invite_api_workspace_invites__invite_id__delete` |
| `GET` | `/api/workspace/members` | `members_api_workspace_members_get` |
| `DELETE` | `/api/workspace/members/{user_id}` | `remove_member_api_workspace_members__user_id__delete` |
| `PUT` | `/api/workspace/members/{user_id}` | `set_member_role_api_workspace_members__user_id__put` |
| `PUT` | `/api/workspace/settings` | `update_settings_api_workspace_settings_put` |
| `GET` | `/api/workspace/storage` | `workspace_storage_api_workspace_storage_get` |
| `POST` | `/api/workspace/storage/cleanup` | `workspace_storage_cleanup_api_workspace_storage_cleanup_post` |
| `GET` | `/api/workspace/usage` | `workspace_usage_api_workspace_usage_get` |
| `POST` | `/api/workspace/usage/check` | `check_usage_api_workspace_usage_check_post` |
| `GET` | `/api/workspace/usage/history` | `workspace_usage_history_api_workspace_usage_history_get` |
| `PUT` | `/api/workspace/usage/limits` | `update_usage_limits_api_workspace_usage_limits_put` |
| `POST` | `/api/workspace/usage/recount` | `recount_usage_api_workspace_usage_recount_post` |
| `GET` | `/api/workspaces` | `list_workspaces_api_workspaces_get` |
| `POST` | `/api/workspaces` | `create_workspace_api_workspaces_post` |
| `POST` | `/api/workspaces/join` | `join_workspace_api_workspaces_join_post` |
