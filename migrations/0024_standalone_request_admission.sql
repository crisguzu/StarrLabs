-- No raw email/IP is stored in this retained request-admission table.
CREATE TABLE IF NOT EXISTS public.standalone_request_admissions (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, operation text NOT NULL, subject_digest text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS standalone_request_admissions_window_idx ON public.standalone_request_admissions(operation,created_at,subject_digest);
