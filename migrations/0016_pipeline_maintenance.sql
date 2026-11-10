-- Pipeline maintenance: make poll retention safe and persist health/readiness audits.

ALTER TABLE public.battle_sources
  DROP CONSTRAINT IF EXISTS battle_sources_source_poll_id_fkey,
  ADD CONSTRAINT battle_sources_source_poll_id_fkey
    FOREIGN KEY (source_poll_id) REFERENCES public.collector_polls(id) ON DELETE SET NULL;

ALTER TABLE public.official_reference_snapshots
  DROP CONSTRAINT IF EXISTS official_reference_snapshots_source_poll_id_fkey,
  ADD CONSTRAINT official_reference_snapshots_source_poll_id_fkey
    FOREIGN KEY (source_poll_id) REFERENCES public.collector_polls(id) ON DELETE SET NULL;

ALTER TABLE public.player_profile_snapshots
  DROP CONSTRAINT IF EXISTS player_profile_snapshots_source_poll_id_fkey,
  ADD CONSTRAINT player_profile_snapshots_source_poll_id_fkey
    FOREIGN KEY (source_poll_id) REFERENCES public.collector_polls(id) ON DELETE SET NULL;

ALTER TABLE public.reciprocal_battlelog_requests
  DROP CONSTRAINT IF EXISTS reciprocal_battlelog_requests_source_poll_id_fkey,
  ADD CONSTRAINT reciprocal_battlelog_requests_source_poll_id_fkey
    FOREIGN KEY (source_poll_id) REFERENCES public.collector_polls(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS public.pipeline_health_snapshots (
  id bigserial PRIMARY KEY,
  captured_at timestamptz NOT NULL DEFAULT now(),
  healthy boolean NOT NULL,
  retraining_ready boolean NOT NULL,
  metrics jsonb NOT NULL,
  warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
  blockers jsonb NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_pipeline_health_snapshots_captured
  ON public.pipeline_health_snapshots(captured_at DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON public.pipeline_health_snapshots
TO CURRENT_USER;
GRANT SELECT ON public.pipeline_health_snapshots TO CURRENT_USER;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public
TO CURRENT_USER;

INSERT INTO public.schema_migrations(version)
VALUES ('0016_pipeline_maintenance')
ON CONFLICT (version) DO NOTHING;
