-- Phase 4: multi-user tracking, point-in-time profiles, collection telemetry,
-- immutable battle provenance, official reference snapshots, and retention support.

CREATE OR REPLACE VIEW public.tracked_players AS
SELECT DISTINCT m.player_tag
FROM public.player_list_members AS m
JOIN public.player_lists AS l ON l.id = m.list_id
WHERE NOT l.is_deleted
  AND NOT m.is_removed;

CREATE TABLE IF NOT EXISTS public.schema_migrations (
  version text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.collector_polls (
  id bigserial PRIMARY KEY,
  player_tag text,
  endpoint text NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  http_status integer,
  api_latency_ms integer,
  returned_items integer,
  new_items integer,
  duplicate_items integer,
  oldest_battle_time timestamptz,
  newest_battle_time timestamptz,
  cache_max_age_seconds integer,
  retry_after_seconds integer,
  rate_limited boolean NOT NULL DEFAULT false,
  success boolean NOT NULL DEFAULT false,
  possible_gap boolean NOT NULL DEFAULT false,
  error_category text,
  error_message text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_collector_polls_player_started
  ON public.collector_polls(player_tag, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_collector_polls_endpoint_started
  ON public.collector_polls(endpoint, started_at DESC);

CREATE TABLE IF NOT EXISTS public.tracked_player_state (
  player_tag text PRIMARY KEY,
  last_battle_poll_at timestamptz,
  last_successful_battle_poll_at timestamptz,
  last_profile_poll_at timestamptz,
  last_successful_profile_poll_at timestamptz,
  newest_battle_time timestamptz,
  consecutive_high_velocity integer NOT NULL DEFAULT 0,
  polling_interval_seconds integer NOT NULL DEFAULT 900,
  next_battle_poll_at timestamptz,
  next_profile_poll_at timestamptz,
  possible_gap boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.player_profile_snapshots (
  id bigserial PRIMARY KEY,
  player_tag text NOT NULL,
  captured_at timestamptz NOT NULL DEFAULT now(),
  source text NOT NULL DEFAULT 'supercell_player_api',
  source_poll_id bigint REFERENCES public.collector_polls(id),
  payload_hash text NOT NULL,
  payload jsonb NOT NULL,
  name text,
  icon_id integer,
  trophies integer,
  highest_trophies integer,
  exp_level integer,
  exp_points integer,
  victories_3v3 integer,
  solo_victories integer,
  duo_victories integer,
  club_tag text,
  club_name text,
  ranked_elo integer,
  ranked_rank integer,
  ranked_rank_name text,
  ranked_season_id integer,
  highest_season_ranked_elo integer,
  highest_season_ranked_rank integer,
  highest_season_ranked_rank_name text,
  highest_all_time_ranked_elo integer,
  highest_all_time_ranked_rank integer,
  highest_all_time_ranked_rank_name text,
  total_prestige_level integer,
  historical_precision text NOT NULL DEFAULT 'timestamped'
);

CREATE INDEX IF NOT EXISTS idx_profile_snapshots_player_captured
  ON public.player_profile_snapshots(player_tag, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_profile_snapshots_hash
  ON public.player_profile_snapshots(player_tag, payload_hash);

CREATE TABLE IF NOT EXISTS public.player_brawler_snapshots (
  profile_snapshot_id bigint NOT NULL REFERENCES public.player_profile_snapshots(id) ON DELETE CASCADE,
  player_tag text NOT NULL,
  captured_at timestamptz NOT NULL,
  brawler_id integer NOT NULL,
  brawler_name text,
  power integer,
  rank integer,
  trophies integer,
  highest_trophies integer,
  prestige_level integer,
  current_win_streak integer,
  max_win_streak integer,
  skin_json jsonb,
  star_powers_json jsonb,
  gadgets_json jsonb,
  gears_json jsonb,
  hypercharges_json jsonb,
  buffies_json jsonb,
  PRIMARY KEY (profile_snapshot_id, brawler_id)
);

CREATE INDEX IF NOT EXISTS idx_brawler_snapshots_player_brawler_captured
  ON public.player_brawler_snapshots(player_tag, brawler_id, captured_at DESC);

ALTER TABLE public.player_profiles
  ADD COLUMN IF NOT EXISTS ranked_elo integer,
  ADD COLUMN IF NOT EXISTS ranked_rank integer,
  ADD COLUMN IF NOT EXISTS ranked_rank_name text,
  ADD COLUMN IF NOT EXISTS ranked_season_id integer,
  ADD COLUMN IF NOT EXISTS highest_season_ranked_elo integer,
  ADD COLUMN IF NOT EXISTS highest_season_ranked_rank integer,
  ADD COLUMN IF NOT EXISTS highest_season_ranked_rank_name text,
  ADD COLUMN IF NOT EXISTS highest_all_time_ranked_elo integer,
  ADD COLUMN IF NOT EXISTS highest_all_time_ranked_rank integer,
  ADD COLUMN IF NOT EXISTS highest_all_time_ranked_rank_name text,
  ADD COLUMN IF NOT EXISTS total_prestige_level integer;

ALTER TABLE public.player_brawlers
  ADD COLUMN IF NOT EXISTS prestige_level integer,
  ADD COLUMN IF NOT EXISTS current_win_streak integer,
  ADD COLUMN IF NOT EXISTS max_win_streak integer,
  ADD COLUMN IF NOT EXISTS skin_json jsonb,
  ADD COLUMN IF NOT EXISTS hypercharges_json jsonb,
  ADD COLUMN IF NOT EXISTS buffies_json jsonb;

ALTER TABLE public.battles
  ADD COLUMN IF NOT EXISTS first_collected_at timestamptz,
  ADD COLUMN IF NOT EXISTS last_observed_at timestamptz,
  ADD COLUMN IF NOT EXISTS source_poll_id bigint,
  ADD COLUMN IF NOT EXISTS observed_from_tag text,
  ADD COLUMN IF NOT EXISTS team_index integer,
  ADD COLUMN IF NOT EXISTS raw_battle_hash text,
  ADD COLUMN IF NOT EXISTS possible_gap boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS legacy_profile_enrichment boolean NOT NULL DEFAULT true;

UPDATE public.battles
SET first_collected_at = COALESCE(first_collected_at, battletime_ts, battletime::timestamptz),
    last_observed_at = COALESCE(last_observed_at, battletime_ts, battletime::timestamptz)
WHERE first_collected_at IS NULL OR last_observed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_battles_observed_from_time
  ON public.battles(observed_from_tag, battletime_ts DESC);

CREATE TABLE IF NOT EXISTS public.battle_sources (
  battletime text NOT NULL,
  participant_tag text NOT NULL,
  observed_from_tag text NOT NULL,
  source_poll_id bigint REFERENCES public.collector_polls(id),
  first_observed_at timestamptz NOT NULL DEFAULT now(),
  last_observed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (battletime, participant_tag, observed_from_tag)
);

CREATE TABLE IF NOT EXISTS public.official_reference_snapshots (
  id bigserial PRIMARY KEY,
  endpoint text NOT NULL,
  scope_key text NOT NULL DEFAULT '',
  captured_at timestamptz NOT NULL DEFAULT now(),
  source_poll_id bigint REFERENCES public.collector_polls(id),
  payload_hash text NOT NULL,
  payload jsonb NOT NULL,
  UNIQUE (endpoint, scope_key, payload_hash)
);

CREATE INDEX IF NOT EXISTS idx_reference_snapshots_endpoint_captured
  ON public.official_reference_snapshots(endpoint, scope_key, captured_at DESC);

-- Preserve current mutable state as an explicitly current-only legacy baseline.
INSERT INTO public.player_profile_snapshots (
  player_tag, captured_at, source, payload_hash, payload,
  name, icon_id, trophies, highest_trophies, exp_level, exp_points,
  victories_3v3, solo_victories, duo_victories, club_tag, club_name,
  historical_precision
)
SELECT
  p.tag, now(), 'legacy_current_state', md5(row_to_json(p)::text), row_to_json(p)::jsonb,
  p.name, p.icon_id, p.trophies, p.highest_trophies, p.exp_level, p.exp_points,
  p."3v3_victories", p.solo_victories, p.duo_victories, p.club_tag, p.club_name,
  'current_only'
FROM public.player_profiles AS p
WHERE NOT EXISTS (
  SELECT 1 FROM public.player_profile_snapshots s
  WHERE s.player_tag = p.tag AND s.source = 'legacy_current_state'
);

INSERT INTO public.schema_migrations(version)
VALUES ('0002_phase4_collection_and_snapshots')
ON CONFLICT (version) DO NOTHING;
