-- Phase 4: point-in-time reference versions and derived mechanics/geometry.

CREATE TABLE IF NOT EXISTS public.game_data_versions (
  id bigserial PRIMARY KEY,
  provider text NOT NULL,
  dataset_path text NOT NULL,
  snapshot_id bigint NOT NULL REFERENCES public.third_party_reference_snapshots(id) ON DELETE RESTRICT,
  payload_hash text NOT NULL,
  schema_fingerprint text NOT NULL,
  effective_from timestamptz NOT NULL,
  effective_to timestamptz,
  known_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider, dataset_path, snapshot_id)
);

CREATE INDEX IF NOT EXISTS idx_game_data_versions_time
  ON public.game_data_versions(provider, dataset_path, effective_from, effective_to);
CREATE INDEX IF NOT EXISTS idx_game_data_versions_known
  ON public.game_data_versions(provider, dataset_path, known_at);

CREATE TABLE IF NOT EXISTS public.reference_derivation_runs (
  id bigserial PRIMARY KEY,
  provider text NOT NULL,
  input_bundle_hash text NOT NULL UNIQUE,
  effective_from timestamptz NOT NULL,
  effective_to timestamptz,
  known_at timestamptz NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  status text NOT NULL CHECK (status IN ('running', 'complete', 'failed')),
  input_snapshot_ids jsonb NOT NULL,
  metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
  error_message text
);

CREATE TABLE IF NOT EXISTS public.brawler_mechanics_versions (
  derivation_run_id bigint NOT NULL REFERENCES public.reference_derivation_runs(id) ON DELETE RESTRICT,
  brawler_id integer NOT NULL,
  internal_name text NOT NULL,
  display_name text,
  class_archetype text,
  movement_type text,
  speed integer,
  base_hitpoints integer,
  attack_range integer,
  attack_damage integer,
  attack_bullets integer,
  reload_ms integer,
  super_range integer,
  super_damage integer,
  super_bullets integer,
  can_walk_over_water boolean,
  offense_rating integer,
  defense_rating integer,
  utility_rating integer,
  traits jsonb NOT NULL DEFAULT '[]'::jsonb,
  mechanics jsonb NOT NULL DEFAULT '{}'::jsonb,
  coverage_confidence numeric(5,4) NOT NULL,
  missing_fields jsonb NOT NULL DEFAULT '[]'::jsonb,
  PRIMARY KEY (derivation_run_id, brawler_id)
);

CREATE INDEX IF NOT EXISTS idx_brawler_mechanics_id
  ON public.brawler_mechanics_versions(brawler_id, derivation_run_id);

CREATE TABLE IF NOT EXISTS public.map_geometry_versions (
  derivation_run_id bigint NOT NULL REFERENCES public.reference_derivation_runs(id) ON DELETE RESTRICT,
  location_id integer NOT NULL,
  internal_location_name text NOT NULL,
  display_name text,
  game_mode_variation text,
  internal_map_name text NOT NULL,
  width integer NOT NULL,
  height integer NOT NULL,
  tile_count integer NOT NULL,
  open_ratio numeric(8,6),
  movement_blocked_ratio numeric(8,6),
  projectile_blocked_ratio numeric(8,6),
  destructible_ratio numeric(8,6),
  bush_ratio numeric(8,6),
  water_ratio numeric(8,6),
  damaging_ratio numeric(8,6),
  speed_effect_ratio numeric(8,6),
  choke_ratio numeric(8,6),
  largest_walkable_component_ratio numeric(8,6),
  horizontal_symmetry numeric(8,6),
  vertical_symmetry numeric(8,6),
  longest_sightline integer,
  box_marker_count integer NOT NULL DEFAULT 0,
  spawn_marker_count integer NOT NULL DEFAULT 0,
  unknown_tile_ratio numeric(8,6),
  tile_counts jsonb NOT NULL,
  geometry jsonb NOT NULL DEFAULT '{}'::jsonb,
  coverage_confidence numeric(5,4) NOT NULL,
  PRIMARY KEY (derivation_run_id, location_id)
);

CREATE INDEX IF NOT EXISTS idx_map_geometry_location
  ON public.map_geometry_versions(location_id, derivation_run_id);

CREATE TABLE IF NOT EXISTS public.reference_balance_changes (
  id bigserial PRIMARY KEY,
  derivation_run_id bigint NOT NULL REFERENCES public.reference_derivation_runs(id) ON DELETE RESTRICT,
  effective_at timestamptz NOT NULL,
  known_at timestamptz NOT NULL,
  entity_type text NOT NULL CHECK (entity_type IN ('brawler', 'map')),
  entity_id integer NOT NULL,
  field_name text NOT NULL,
  old_value jsonb,
  new_value jsonb,
  source text NOT NULL DEFAULT 'snapshot_diff',
  UNIQUE (derivation_run_id, entity_type, entity_id, field_name)
);

CREATE OR REPLACE VIEW public.current_brawler_mechanics AS
SELECT b.*, r.effective_from, r.effective_to, r.known_at
FROM public.brawler_mechanics_versions b
JOIN public.reference_derivation_runs r ON r.id = b.derivation_run_id
WHERE r.status = 'complete' AND r.effective_to IS NULL;

CREATE OR REPLACE VIEW public.current_map_geometry AS
SELECT m.*, r.effective_from, r.effective_to, r.known_at
FROM public.map_geometry_versions m
JOIN public.reference_derivation_runs r ON r.id = m.derivation_run_id
WHERE r.status = 'complete' AND r.effective_to IS NULL;

CREATE OR REPLACE FUNCTION public.reference_derivation_run_at(
  p_event_time timestamptz,
  p_known_by timestamptz
) RETURNS bigint
LANGUAGE sql
STABLE
AS $$
  SELECT r.id
  FROM public.reference_derivation_runs r
  WHERE r.status = 'complete'
    AND r.effective_from <= p_event_time
    AND (r.effective_to IS NULL OR p_event_time < r.effective_to)
    AND r.known_at <= p_known_by
  ORDER BY r.effective_from DESC, r.id DESC
  LIMIT 1
$$;

GRANT SELECT, INSERT, UPDATE, DELETE ON
  public.game_data_versions,
  public.reference_derivation_runs,
  public.brawler_mechanics_versions,
  public.map_geometry_versions,
  public.reference_balance_changes
TO CURRENT_USER;

GRANT SELECT ON
  public.game_data_versions,
  public.reference_derivation_runs,
  public.brawler_mechanics_versions,
  public.map_geometry_versions,
  public.reference_balance_changes,
  public.current_brawler_mechanics,
  public.current_map_geometry
TO CURRENT_USER;

GRANT SELECT ON public.current_brawler_mechanics, public.current_map_geometry
TO CURRENT_USER;

GRANT EXECUTE ON FUNCTION public.reference_derivation_run_at(timestamptz, timestamptz)
TO CURRENT_USER;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public
TO CURRENT_USER;

INSERT INTO public.schema_migrations(version)
VALUES ('0015_point_in_time_game_data')
ON CONFLICT (version) DO NOTHING;
