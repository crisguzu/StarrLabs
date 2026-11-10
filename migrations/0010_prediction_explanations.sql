ALTER TABLE public.battle_features_3v3
  ADD COLUMN IF NOT EXISTS pred_explanation jsonb;

CREATE INDEX IF NOT EXISTS battle_features_3v3_pred_explanation_version_idx
  ON public.battle_features_3v3 ((pred_explanation->>'model_version'))
  WHERE pred_explanation IS NOT NULL;
