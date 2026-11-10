ALTER TABLE public.player_victory_rollups
  ADD COLUMN IF NOT EXISTS victory_score_version text,
  ADD COLUMN IF NOT EXISTS victory_score_v1 double precision,
  ADD COLUMN IF NOT EXISTS adjusted_lift double precision,
  ADD COLUMN IF NOT EXISTS evidence_confidence double precision,
  ADD COLUMN IF NOT EXISTS posterior_sd double precision,
  ADD COLUMN IF NOT EXISTS rating_prior_sd double precision;

COMMENT ON COLUMN public.player_victory_rollups.victory_score IS
  'Current user-facing Victory Score. Version identified by victory_score_version.';
COMMENT ON COLUMN public.player_victory_rollups.adjusted_lift IS
  'Bayesian posterior-adjusted win-probability lift versus published match expectations.';
