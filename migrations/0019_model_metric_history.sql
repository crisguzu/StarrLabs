-- Normalized time-series ledger for model readiness and evaluation metrics.

CREATE TABLE IF NOT EXISTS public.model_metric_history (
  id bigserial PRIMARY KEY,
  captured_at timestamptz NOT NULL,
  model_key text NOT NULL,
  metric_name text NOT NULL,
  metric_value double precision NOT NULL,
  numerator double precision,
  denominator double precision,
  threshold_value double precision,
  threshold_operator text CHECK (threshold_operator IN ('gte', 'lte', 'eq')),
  threshold_passed boolean,
  unit text NOT NULL DEFAULT 'count',
  phase text,
  source_type text NOT NULL DEFAULT 'pipeline_health_audit',
  source_id bigint REFERENCES public.pipeline_health_snapshots(id) ON DELETE SET NULL,
  dimensions jsonb NOT NULL DEFAULT '{}'::jsonb,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((threshold_value IS NULL AND threshold_operator IS NULL AND threshold_passed IS NULL)
      OR (threshold_value IS NOT NULL AND threshold_operator IS NOT NULL AND threshold_passed IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_model_metric_history_series
  ON public.model_metric_history(model_key, metric_name, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_model_metric_history_captured
  ON public.model_metric_history(captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_model_metric_history_dimensions
  ON public.model_metric_history USING gin(dimensions);

-- Preserve the readiness history already captured in JSON by migration 0016.
INSERT INTO public.model_metric_history
  (captured_at, model_key, metric_name, metric_value, numerator, denominator,
   threshold_value, threshold_operator, threshold_passed, unit, phase,
   source_id, dimensions, metadata)
SELECT h.captured_at, v.model_key, v.metric_name, v.metric_value,
       v.numerator, v.denominator, v.threshold_value, 'gte',
       v.metric_value >= v.threshold_value, v.unit, v.phase, h.id,
       '{"dataset":"production","window":"cumulative"}'::jsonb,
       '{"backfilled_from":"pipeline_health_snapshots.metrics"}'::jsonb
FROM public.pipeline_health_snapshots h
CROSS JOIN LATERAL (VALUES
  ('solo_showdown_top4', 'eligible_lobbies',
    (h.metrics #>> '{reciprocal,solo_lobbies}')::double precision, NULL::double precision, NULL::double precision,
    1000::double precision, 'count', 'evidence_collection'),
  ('solo_showdown_top4', 'coverage_days',
    (h.metrics #>> '{reciprocal,coverage_days}')::double precision, NULL, NULL,
    30, 'days', 'evidence_collection'),
  ('solo_showdown_top4', 'focal_players',
    (h.metrics #>> '{reciprocal,focal_players}')::double precision, NULL, NULL,
    50, 'count', 'evidence_collection'),
  ('solo_showdown_top4', 'reciprocal_match_rate',
    (h.metrics #>> '{reciprocal,match_rate}')::double precision,
    (h.metrics #>> '{reciprocal,matched}')::double precision,
    (h.metrics #>> '{reciprocal,requests}')::double precision,
    0.90, 'ratio', 'evidence_collection'),
  ('loadout_availability_3v3', 'eligible_lobbies',
    (h.metrics #>> '{item_model,eligible_lobbies}')::double precision, NULL, NULL,
    10000, 'count', 'phase_a_collection'),
  ('loadout_availability_3v3', 'coverage_days',
    (h.metrics #>> '{item_model,coverage_days}')::double precision, NULL, NULL,
    30, 'days', 'phase_a_collection'),
  ('loadout_availability_3v3', 'focal_players',
    (h.metrics #>> '{item_model,focal_players}')::double precision, NULL, NULL,
    100, 'count', 'phase_a_collection'),
  ('loadout_availability_3v3', 'participant_coverage',
    (h.metrics #>> '{item_model,participant_coverage}')::double precision,
    (h.metrics #>> '{item_model,covered_participants}')::double precision,
    (h.metrics #>> '{item_model,participants}')::double precision,
    0.70, 'ratio', 'phase_a_collection'),
  ('loadout_availability_3v3', 'full_lobby_coverage',
    (h.metrics #>> '{item_model,full_lobby_coverage}')::double precision,
    (h.metrics #>> '{item_model,full_coverage_lobbies}')::double precision,
    (h.metrics #>> '{item_model,eligible_lobbies}')::double precision,
    0.50, 'ratio', 'phase_a_collection')
) AS v(model_key, metric_name, metric_value, numerator, denominator,
       threshold_value, unit, phase)
WHERE v.metric_value IS NOT NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON public.model_metric_history
TO CURRENT_USER;
GRANT SELECT ON public.model_metric_history TO CURRENT_USER;
GRANT USAGE, SELECT ON SEQUENCE public.model_metric_history_id_seq
TO CURRENT_USER;

INSERT INTO public.schema_migrations(version)
VALUES ('0019_model_metric_history')
ON CONFLICT (version) DO NOTHING;
