-- Trusted post-battle bot/human labels and immutable explainable dataset exports.

CREATE TABLE IF NOT EXISTS public.bot_lobby_labels (
  id bigserial PRIMARY KEY,
  observed_from_tag text NOT NULL,
  battle_time text NOT NULL,
  label text NOT NULL CHECK (label IN ('bots', 'humans', 'mixed', 'unsure')),
  source text NOT NULL DEFAULT 'human_report',
  confidence text NOT NULL DEFAULT 'trusted' CHECK (confidence IN ('trusted', 'probable', 'weak')),
  notes text,
  labeled_by text,
  labeled_at timestamptz NOT NULL DEFAULT now(),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (observed_from_tag, battle_time, source)
);

CREATE TABLE IF NOT EXISTS public.bot_detection_dataset_exports (
  id bigserial PRIMARY KEY,
  generated_at timestamptz NOT NULL DEFAULT now(),
  row_count integer NOT NULL,
  lobby_count integer NOT NULL,
  sha256 text NOT NULL,
  csv_path text NOT NULL,
  jsonl_path text NOT NULL,
  rules_version text NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_bot_lobby_labels_time ON public.bot_lobby_labels(battle_time DESC);

GRANT SELECT, INSERT, UPDATE ON public.bot_lobby_labels, public.bot_detection_dataset_exports
TO CURRENT_USER;
GRANT SELECT ON public.bot_lobby_labels, public.bot_detection_dataset_exports
TO CURRENT_USER;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO CURRENT_USER;

INSERT INTO public.schema_migrations(version)
VALUES ('0022_bot_lobby_labels')
ON CONFLICT (version) DO NOTHING;
