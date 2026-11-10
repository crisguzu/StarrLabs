-- Phase 1/2: immutable raw battle-log responses and bounded reciprocal Solo logs.

ALTER TABLE public.battles
  ADD COLUMN IF NOT EXISTS event_mode_id integer,
  ADD COLUMN IF NOT EXISTS battle_mode text,
  ADD COLUMN IF NOT EXISTS pre_battle_brawler_trophies integer;

CREATE TABLE IF NOT EXISTS public.battlelog_payloads (
  id bigserial PRIMARY KEY,
  source_poll_id bigint REFERENCES public.collector_polls(id) ON DELETE SET NULL,
  observed_from_tag text NOT NULL,
  collection_kind text NOT NULL CHECK (collection_kind IN ('tracked', 'reciprocal')),
  captured_at timestamptz NOT NULL DEFAULT now(),
  payload_hash text NOT NULL,
  payload jsonb NOT NULL,
  returned_items integer NOT NULL DEFAULT 0,
  last_observed_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (observed_from_tag, collection_kind, payload_hash)
);

CREATE INDEX IF NOT EXISTS idx_battlelog_payloads_observer_captured
  ON public.battlelog_payloads(observed_from_tag, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_battlelog_payloads_hash
  ON public.battlelog_payloads(payload_hash);

CREATE TABLE IF NOT EXISTS public.battlelog_payload_observations (
  source_poll_id bigint PRIMARY KEY REFERENCES public.collector_polls(id) ON DELETE CASCADE,
  payload_id bigint NOT NULL REFERENCES public.battlelog_payloads(id) ON DELETE RESTRICT,
  observed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.reciprocal_battlelog_requests (
  id bigserial PRIMARY KEY,
  target_battle_time timestamptz NOT NULL,
  target_battletime text NOT NULL,
  target_event_id integer NOT NULL,
  target_mode text NOT NULL,
  target_roster_hash text NOT NULL,
  discovered_from_tag text NOT NULL,
  opponent_tag text NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'matched', 'missing', 'ambiguous', 'expired', 'failed')),
  attempts integer NOT NULL DEFAULT 0,
  first_requested_at timestamptz NOT NULL DEFAULT now(),
  last_attempted_at timestamptz,
  matched_at timestamptz,
  source_poll_id bigint REFERENCES public.collector_polls(id),
  matched_raw_battle_hash text,
  error_message text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (target_battletime, target_event_id, target_mode, target_roster_hash, opponent_tag)
);

CREATE INDEX IF NOT EXISTS idx_reciprocal_requests_pending
  ON public.reciprocal_battlelog_requests(status, first_requested_at)
  WHERE status IN ('pending', 'missing', 'failed');
CREATE INDEX IF NOT EXISTS idx_reciprocal_requests_target
  ON public.reciprocal_battlelog_requests(target_battle_time, target_roster_hash);

GRANT SELECT, INSERT, UPDATE, DELETE ON
  public.battlelog_payloads,
  public.battlelog_payload_observations,
  public.reciprocal_battlelog_requests
TO CURRENT_USER;

GRANT SELECT ON
  public.battlelog_payloads,
  public.battlelog_payload_observations,
  public.reciprocal_battlelog_requests
TO CURRENT_USER;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public
TO CURRENT_USER;

INSERT INTO public.schema_migrations(version)
VALUES ('0013_raw_battlelogs_and_reciprocal_collection')
ON CONFLICT (version) DO NOTHING;
