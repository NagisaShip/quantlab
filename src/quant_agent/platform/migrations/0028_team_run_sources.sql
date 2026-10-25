-- Host-side provenance for legacy Team plans; never rewrite frozen Runtime input.
CREATE TABLE team_run_sources (
  workspace_id TEXT NOT NULL REFERENCES workspaces(id),
  run_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  evidence TEXT NOT NULL,
  discovered_at BIGINT NOT NULL,
  PRIMARY KEY(workspace_id, run_id, project_id)
);
