-- Phase 1: richer /players/{tag} ingestion

ALTER TABLE public.player_profiles
  ADD COLUMN IF NOT EXISTS name_color text,
  ADD COLUMN IF NOT EXISTS exp_points integer,
  ADD COLUMN IF NOT EXISTS power_play_points integer,
  ADD COLUMN IF NOT EXISTS highest_power_play_points integer,
  ADD COLUMN IF NOT EXISTS is_qualified_from_championship_challenge boolean,
  ADD COLUMN IF NOT EXISTS best_robo_rumble_time integer,
  ADD COLUMN IF NOT EXISTS best_time_as_big_brawler integer;

CREATE TABLE IF NOT EXISTS public.player_brawlers (
  player_tag text NOT NULL,
  brawler_id integer NOT NULL,
  brawler_name text,
  power integer,
  rank integer,
  trophies integer,
  highest_trophies integer,
  star_powers_json jsonb,
  gadgets_json jsonb,
  gears_json jsonb,
  last_update timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (player_tag, brawler_id),
  CONSTRAINT player_brawlers_player_tag_fkey
    FOREIGN KEY (player_tag)
    REFERENCES public.player_profiles(tag)
    ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_player_brawlers_player_tag
  ON public.player_brawlers(player_tag);

CREATE INDEX IF NOT EXISTS idx_player_brawlers_brawler_id
  ON public.player_brawlers(brawler_id);
