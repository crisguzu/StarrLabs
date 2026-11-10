-- Phase B/C: leakage-safe loadout-availability feature stages and build audit.

CREATE TABLE IF NOT EXISTS public.loadout_participant_features_3v3 (
  battle_time timestamptz NOT NULL,
  observed_from_tag text NOT NULL,
  player_tag text NOT NULL,
  team_index integer NOT NULL,
  focal_team_index integer NOT NULL,
  is_focal_player boolean NOT NULL,
  is_focal_team boolean NOT NULL,
  mode text NOT NULL,
  battle_type text NOT NULL,
  map_id bigint,
  brawler_id integer,
  brawler_name text,
  win_label smallint CHECK (win_label IN (0, 1)),
  profile_snapshot_id bigint REFERENCES public.player_profile_snapshots(id) ON DELETE SET NULL,
  snapshot_captured_at timestamptz,
  snapshot_age_seconds bigint,
  has_prebattle_snapshot boolean NOT NULL,
  gadget_count integer,
  star_power_count integer,
  gear_count integer,
  hypercharge_count integer,
  buffy_count integer,
  has_gadget boolean,
  has_star_power boolean,
  has_gear boolean,
  has_hypercharge boolean,
  availability_score numeric(8,6),
  item_reference_total integer NOT NULL DEFAULT 0,
  item_reference_resolved integer NOT NULL DEFAULT 0,
  item_reference_resolution numeric(8,6),
  item_identities jsonb NOT NULL DEFAULT '{}'::jsonb,
  item_identity_signature text,
  brawler_class text,
  brawler_attack_range integer,
  brawler_speed integer,
  map_open_ratio numeric(8,6),
  map_bush_ratio numeric(8,6),
  map_choke_ratio numeric(8,6),
  map_longest_sightline integer,
  reference_confidence numeric(8,6),
  reference_known_at timestamptz,
  feature_version text NOT NULL DEFAULT 'loadout_availability_v1',
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (battle_time, observed_from_tag, player_tag)
);

CREATE INDEX IF NOT EXISTS idx_loadout_participant_snapshot
  ON public.loadout_participant_features_3v3(has_prebattle_snapshot, battle_time DESC);
CREATE INDEX IF NOT EXISTS idx_loadout_participant_brawler
  ON public.loadout_participant_features_3v3(brawler_id, battle_time DESC);

CREATE TABLE IF NOT EXISTS public.loadout_lobby_features_3v3 (
  battle_time timestamptz NOT NULL,
  observed_from_tag text NOT NULL,
  mode text NOT NULL,
  battle_type text NOT NULL,
  map_id bigint,
  focal_team_index integer NOT NULL,
  win_label smallint CHECK (win_label IN (0, 1)),
  participants integer NOT NULL,
  covered_participants integer NOT NULL,
  full_lobby_coverage boolean NOT NULL,
  participant_coverage numeric(8,6) NOT NULL,
  item_reference_total integer NOT NULL,
  item_reference_resolved integer NOT NULL,
  item_reference_resolution numeric(8,6),
  team_gadget_count integer,
  enemy_gadget_count integer,
  diff_gadget_count integer,
  team_star_power_count integer,
  enemy_star_power_count integer,
  diff_star_power_count integer,
  team_gear_count integer,
  enemy_gear_count integer,
  diff_gear_count integer,
  team_hypercharge_count integer,
  enemy_hypercharge_count integer,
  diff_hypercharge_count integer,
  team_buffy_count integer,
  enemy_buffy_count integer,
  diff_buffy_count integer,
  team_availability_score numeric(8,6),
  enemy_availability_score numeric(8,6),
  diff_availability_score numeric(8,6),
  team_item_signature text,
  enemy_item_signature text,
  focal_brawler_id integer,
  focal_item_signature text,
  brawler_class text,
  map_open_ratio numeric(8,6),
  map_bush_ratio numeric(8,6),
  map_choke_ratio numeric(8,6),
  map_longest_sightline integer,
  brawler_attack_range integer,
  brawler_speed integer,
  range_open_interaction numeric(14,6),
  speed_choke_interaction numeric(14,6),
  brawler_item_mode_key text,
  brawler_item_map_key text,
  max_available_stage smallint NOT NULL CHECK (max_available_stage BETWEEN 0 AND 4),
  feature_version text NOT NULL DEFAULT 'loadout_availability_v1',
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (battle_time, observed_from_tag)
);

CREATE INDEX IF NOT EXISTS idx_loadout_lobby_stage
  ON public.loadout_lobby_features_3v3(max_available_stage, battle_time DESC);

CREATE TABLE IF NOT EXISTS public.loadout_feature_builds (
  id bigserial PRIMARY KEY,
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  status text NOT NULL CHECK (status IN ('running', 'complete', 'failed')),
  feature_version text NOT NULL,
  source_cutoff timestamptz NOT NULL,
  participant_rows integer NOT NULL DEFAULT 0,
  lobby_rows integer NOT NULL DEFAULT 0,
  stage_counts jsonb NOT NULL DEFAULT '{}'::jsonb,
  metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
  error_message text
);

CREATE INDEX IF NOT EXISTS idx_loadout_feature_builds_latest
  ON public.loadout_feature_builds(completed_at DESC) WHERE status = 'complete';

GRANT SELECT, INSERT, UPDATE, DELETE ON
  public.loadout_participant_features_3v3,
  public.loadout_lobby_features_3v3,
  public.loadout_feature_builds
TO CURRENT_USER;

GRANT SELECT ON
  public.loadout_participant_features_3v3,
  public.loadout_lobby_features_3v3,
  public.loadout_feature_builds
TO CURRENT_USER;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public
TO CURRENT_USER;

INSERT INTO public.schema_migrations(version)
VALUES ('0020_loadout_feature_stages')
ON CONFLICT (version) DO NOTHING;
