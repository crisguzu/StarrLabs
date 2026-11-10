-- Phase A: bounded direct-opponent profile snapshots for future-battle context.

CREATE TABLE IF NOT EXISTS public.opponent_profile_pool (
  player_tag text PRIMARY KEY,
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'not_found', 'error', 'paused')),
  first_discovered_at timestamptz NOT NULL DEFAULT now(),
  last_discovered_at timestamptz NOT NULL DEFAULT now(),
  last_battle_time timestamptz,
  last_discovered_from_tag text,
  discovery_count integer NOT NULL DEFAULT 1,
  last_profile_poll_at timestamptz,
  last_successful_profile_poll_at timestamptz,
  next_profile_poll_at timestamptz NOT NULL DEFAULT now(),
  attempts integer NOT NULL DEFAULT 0,
  source_poll_id bigint REFERENCES public.collector_polls(id) ON DELETE SET NULL,
  last_error text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_opponent_profile_pool_due
  ON public.opponent_profile_pool(status, next_profile_poll_at, last_discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_opponent_profile_pool_discovered
  ON public.opponent_profile_pool(last_discovered_at DESC);

CREATE TABLE IF NOT EXISTS public.opponent_profile_discoveries (
  id bigserial PRIMARY KEY,
  player_tag text NOT NULL REFERENCES public.opponent_profile_pool(player_tag) ON DELETE CASCADE,
  discovered_from_tag text NOT NULL,
  battle_time timestamptz NOT NULL,
  mode text,
  roster_hash text,
  discovered_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (player_tag, discovered_from_tag, battle_time)
);

CREATE INDEX IF NOT EXISTS idx_opponent_profile_discoveries_battle
  ON public.opponent_profile_discoveries(battle_time DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON
  public.opponent_profile_pool,
  public.opponent_profile_discoveries
TO CURRENT_USER;

GRANT SELECT ON
  public.opponent_profile_pool,
  public.opponent_profile_discoveries
TO CURRENT_USER;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public
TO CURRENT_USER;

INSERT INTO public.schema_migrations(version)
VALUES ('0018_opponent_profile_pool')
ON CONFLICT (version) DO NOTHING;
