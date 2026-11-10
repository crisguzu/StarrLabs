-- Phase D/E: audited offline evaluation, shadow registry, and explicit promotion control.

CREATE TABLE IF NOT EXISTS public.loadout_model_evaluation_runs (
  id bigserial PRIMARY KEY,
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  status text NOT NULL CHECK (status IN ('blocked_by_phase_c','running','complete','failed')),
  feature_version text NOT NULL,
  source_cutoff timestamptz NOT NULL,
  readiness_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  dataset_manifest jsonb NOT NULL DEFAULT '{}'::jsonb,
  dataset_manifest_sha256 text,
  artifact_path text,
  promotion_eligible boolean NOT NULL DEFAULT false,
  blockers jsonb NOT NULL DEFAULT '[]'::jsonb,
  error_message text
);

CREATE TABLE IF NOT EXISTS public.loadout_model_stage_metrics (
  id bigserial PRIMARY KEY,
  evaluation_run_id bigint NOT NULL REFERENCES public.loadout_model_evaluation_runs(id) ON DELETE CASCADE,
  stage smallint NOT NULL CHECK (stage BETWEEN 0 AND 4),
  split_name text NOT NULL,
  fold_index integer NOT NULL DEFAULT 0,
  row_count integer NOT NULL,
  player_count integer,
  roc_auc double precision,
  brier double precision,
  log_loss double precision,
  ece double precision,
  base_rate_brier double precision,
  brier_improvement double precision,
  confidence_interval jsonb NOT NULL DEFAULT '{}'::jsonb,
  subgroup_metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
  passed boolean NOT NULL DEFAULT false,
  UNIQUE(evaluation_run_id,stage,split_name,fold_index)
);

CREATE TABLE IF NOT EXISTS public.loadout_model_registry (
  id bigserial PRIMARY KEY,
  model_version text NOT NULL UNIQUE,
  evaluation_run_id bigint NOT NULL REFERENCES public.loadout_model_evaluation_runs(id),
  status text NOT NULL CHECK (status IN ('evaluated','shadow','rejected','promoted','retired')),
  artifact_path text NOT NULL,
  artifact_sha256 text NOT NULL,
  registered_at timestamptz NOT NULL DEFAULT now(),
  shadow_started_at timestamptz,
  shadow_completed_at timestamptz,
  promoted_at timestamptz,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS public.loadout_shadow_scores (
  model_registry_id bigint NOT NULL REFERENCES public.loadout_model_registry(id) ON DELETE CASCADE,
  battle_time timestamptz NOT NULL,
  observed_from_tag text NOT NULL,
  scored_at timestamptz NOT NULL DEFAULT now(),
  probability double precision NOT NULL CHECK (probability BETWEEN 0 AND 1),
  outcome smallint CHECK (outcome IN (0,1)),
  feature_stage smallint NOT NULL CHECK (feature_stage BETWEEN 0 AND 4),
  feature_manifest_sha256 text NOT NULL,
  PRIMARY KEY(model_registry_id,battle_time,observed_from_tag)
);

CREATE TABLE IF NOT EXISTS public.loadout_promotion_audit (
  id bigserial PRIMARY KEY,
  model_registry_id bigint NOT NULL REFERENCES public.loadout_model_registry(id),
  action text NOT NULL CHECK (action IN ('shadow_start','shadow_reject','promotion_request','promotion_reject','promote','retire')),
  occurred_at timestamptz NOT NULL DEFAULT now(),
  actor text NOT NULL,
  explicit_approval boolean NOT NULL DEFAULT false,
  reason text NOT NULL,
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_loadout_evaluation_latest ON public.loadout_model_evaluation_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_loadout_stage_metrics_run ON public.loadout_model_stage_metrics(evaluation_run_id,stage,split_name);
CREATE INDEX IF NOT EXISTS idx_loadout_registry_status ON public.loadout_model_registry(status,registered_at DESC);
CREATE INDEX IF NOT EXISTS idx_loadout_shadow_scored ON public.loadout_shadow_scores(model_registry_id,scored_at DESC);

GRANT SELECT,INSERT,UPDATE,DELETE ON
  public.loadout_model_evaluation_runs, public.loadout_model_stage_metrics,
  public.loadout_model_registry, public.loadout_shadow_scores,
  public.loadout_promotion_audit TO CURRENT_USER;
GRANT SELECT ON
  public.loadout_model_evaluation_runs, public.loadout_model_stage_metrics,
  public.loadout_model_registry, public.loadout_shadow_scores,
  public.loadout_promotion_audit TO CURRENT_USER;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO CURRENT_USER;

INSERT INTO public.schema_migrations(version)
VALUES ('0021_loadout_evaluation_rollout') ON CONFLICT (version) DO NOTHING;
