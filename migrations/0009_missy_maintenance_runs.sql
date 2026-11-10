CREATE TABLE IF NOT EXISTS missy_maintenance_runs (
  id uuid PRIMARY KEY,
  job_type text NOT NULL CHECK (job_type IN ('predictions', 'victory', 'all')),
  trigger_type text NOT NULL CHECK (trigger_type IN ('manual', 'scheduled')),
  requested_by text,
  status text NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'skipped')),
  requested_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  duration_seconds numeric,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  error_message text
);

CREATE INDEX IF NOT EXISTS missy_maintenance_runs_requested_idx
  ON missy_maintenance_runs(requested_at DESC);
CREATE INDEX IF NOT EXISTS missy_maintenance_runs_active_idx
  ON missy_maintenance_runs(status) WHERE status IN ('queued', 'running');

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brawl_prod_app') THEN
    GRANT SELECT, INSERT, UPDATE ON missy_maintenance_runs TO CURRENT_USER;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brawl_dev_app') THEN
    GRANT SELECT, INSERT, UPDATE ON missy_maintenance_runs TO CURRENT_USER;
  END IF;
END $$;
