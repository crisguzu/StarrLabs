-- Persistent recovery queue for post-match opponent profile scouting.

CREATE TABLE IF NOT EXISTS public.scout_profile_retry_queue (
  player_tag text PRIMARY KEY,
  observed_from_tag text,
  battle_time text,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'rate_limited', 'error', 'succeeded', 'not_found')),
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  next_retry_at timestamptz,
  last_http_status integer,
  last_retry_after_seconds double precision,
  last_error text,
  first_queued_at timestamptz NOT NULL DEFAULT now(),
  last_attempt_at timestamptz,
  resolved_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_scout_profile_retry_due
ON public.scout_profile_retry_queue(next_retry_at)
WHERE status IN ('pending', 'rate_limited', 'error');

GRANT SELECT, INSERT, UPDATE ON public.scout_profile_retry_queue
TO CURRENT_USER;
GRANT SELECT ON public.scout_profile_retry_queue TO CURRENT_USER;

INSERT INTO public.schema_migrations(version)
VALUES ('0023_scout_profile_retry_queue')
ON CONFLICT (version) DO NOTHING;
