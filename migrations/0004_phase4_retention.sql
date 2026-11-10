-- Phase 4 long-term progression aggregates used before detailed snapshot expiry.

CREATE TABLE IF NOT EXISTS public.player_monthly_progression (
  player_tag text NOT NULL,
  month date NOT NULL,
  first_snapshot_at timestamptz,
  last_snapshot_at timestamptz,
  min_trophies integer,
  max_trophies integer,
  ending_trophies integer,
  ending_ranked_elo integer,
  ending_ranked_rank integer,
  ending_total_prestige_level integer,
  snapshot_count integer NOT NULL,
  PRIMARY KEY (player_tag, month)
);

CREATE TABLE IF NOT EXISTS public.brawler_monthly_progression (
  player_tag text NOT NULL,
  brawler_id integer NOT NULL,
  month date NOT NULL,
  brawler_name text,
  min_trophies integer,
  max_trophies integer,
  ending_trophies integer,
  ending_power integer,
  ending_rank integer,
  ending_prestige_level integer,
  max_win_streak integer,
  snapshot_count integer NOT NULL,
  PRIMARY KEY (player_tag, brawler_id, month)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON
  public.player_monthly_progression,
  public.brawler_monthly_progression
TO CURRENT_USER;

GRANT SELECT ON
  public.player_monthly_progression,
  public.brawler_monthly_progression
TO CURRENT_USER;

INSERT INTO public.schema_migrations(version)
VALUES ('0004_phase4_retention')
ON CONFLICT (version) DO NOTHING;
