-- Phase 3: immutable, content-addressed third-party game/reference data.

CREATE TABLE IF NOT EXISTS public.third_party_reference_snapshots (
  id bigserial PRIMARY KEY,
  provider text NOT NULL,
  dataset_path text NOT NULL,
  source_poll_id bigint REFERENCES public.collector_polls(id) ON DELETE SET NULL,
  first_captured_at timestamptz NOT NULL DEFAULT now(),
  last_observed_at timestamptz NOT NULL DEFAULT now(),
  upstream_generated_at timestamptz,
  upstream_etag text,
  payload_hash text NOT NULL,
  schema_fingerprint text NOT NULL,
  payload_bytes bigint NOT NULL,
  row_count integer,
  payload jsonb NOT NULL,
  UNIQUE (provider, dataset_path, payload_hash)
);

CREATE INDEX IF NOT EXISTS idx_third_party_reference_latest
  ON public.third_party_reference_snapshots(provider, dataset_path, last_observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_third_party_reference_schema
  ON public.third_party_reference_snapshots(provider, dataset_path, schema_fingerprint);

CREATE TABLE IF NOT EXISTS public.third_party_reference_observations (
  source_poll_id bigint PRIMARY KEY REFERENCES public.collector_polls(id) ON DELETE CASCADE,
  snapshot_id bigint NOT NULL REFERENCES public.third_party_reference_snapshots(id) ON DELETE RESTRICT,
  observed_at timestamptz NOT NULL DEFAULT now(),
  http_status integer NOT NULL,
  not_modified boolean NOT NULL DEFAULT false,
  schema_changed boolean NOT NULL DEFAULT false,
  response_etag text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

GRANT SELECT, INSERT, UPDATE, DELETE ON
  public.third_party_reference_snapshots,
  public.third_party_reference_observations
TO CURRENT_USER;

GRANT SELECT ON
  public.third_party_reference_snapshots,
  public.third_party_reference_observations
TO CURRENT_USER;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public
TO CURRENT_USER;

INSERT INTO public.schema_migrations(version)
VALUES ('0014_brawlapi_reference_snapshots')
ON CONFLICT (version) DO NOTHING;
