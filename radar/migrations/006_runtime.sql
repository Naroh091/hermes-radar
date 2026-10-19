ALTER TABLE plans ADD COLUMN source_id TEXT;

CREATE INDEX IF NOT EXISTS idx_plans_source
 ON plans(profile_key, owner_id, source_id, created_at);
CREATE INDEX IF NOT EXISTS idx_followups_runtime_due
 ON followups(profile_key, owner_id, status, next_check_at, expires_at);
CREATE INDEX IF NOT EXISTS idx_runs_execution_status
 ON runs(profile_key, owner_id, execution_key, status);
CREATE INDEX IF NOT EXISTS idx_budgets_lookup
 ON budgets(profile_key, owner_id, bucket_type, bucket_start, resource_key, unit);
