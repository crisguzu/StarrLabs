CREATE TABLE IF NOT EXISTS public.player_victory_score_history (
  snapshot_date date NOT NULL DEFAULT current_date,
  captured_at timestamptz NOT NULL DEFAULT now(),
  player_tag text NOT NULL,
  window_days integer NOT NULL,
  mode text NOT NULL,
  type text NOT NULL,
  brawler_name text NOT NULL,
  victory_score double precision,
  victory_score_version text,
  n_battles integer NOT NULL,
  wins integer NOT NULL,
  expected_wins double precision NOT NULL,
  adjusted_lift double precision,
  evidence_confidence double precision,
  PRIMARY KEY (snapshot_date, player_tag, window_days, mode, type, brawler_name)
);

CREATE INDEX IF NOT EXISTS idx_player_victory_score_history_player_date
  ON public.player_victory_score_history (player_tag, snapshot_date DESC);

COMMENT ON TABLE public.player_victory_score_history IS
  'Genuine daily Victory Score V2 snapshots. Never reconstruct historical scores from current model outputs.';

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brawl_prod_app') THEN
    GRANT SELECT, INSERT, UPDATE ON player_victory_score_history TO CURRENT_USER;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brawl_dev_app') THEN
    GRANT SELECT, INSERT, UPDATE ON player_victory_score_history TO CURRENT_USER;
  END IF;
END $$;
