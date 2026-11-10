CREATE TABLE IF NOT EXISTS starrlabs_email_login_challenges (
  id uuid PRIMARY KEY,
  identity_email text NOT NULL,
  code_digest char(64) NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'verified', 'expired', 'cancelled', 'delivery_failed')),
  next_path text NOT NULL DEFAULT '/',
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  requested_ip_hash char(64),
  user_agent_hash char(64),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  consumed_at timestamptz,
  delivery_id text
);

CREATE INDEX IF NOT EXISTS starrlabs_email_login_email_created_idx
  ON starrlabs_email_login_challenges(identity_email, created_at DESC);
CREATE INDEX IF NOT EXISTS starrlabs_email_login_ip_created_idx
  ON starrlabs_email_login_challenges(requested_ip_hash, created_at DESC);
CREATE INDEX IF NOT EXISTS starrlabs_email_login_expiry_idx
  ON starrlabs_email_login_challenges(status, expires_at);

CREATE TABLE IF NOT EXISTS starrlabs_identity_sessions (
  id uuid PRIMARY KEY,
  token_digest char(64) NOT NULL UNIQUE,
  identity_email text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz,
  created_ip_hash char(64),
  user_agent_hash char(64)
);

CREATE INDEX IF NOT EXISTS starrlabs_identity_sessions_email_idx
  ON starrlabs_identity_sessions(identity_email, expires_at DESC);
CREATE INDEX IF NOT EXISTS starrlabs_identity_sessions_active_idx
  ON starrlabs_identity_sessions(token_digest, expires_at)
  WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS starrlabs_auth_audit (
  id bigserial PRIMARY KEY,
  identity_email text,
  action text NOT NULL CHECK (action IN (
    'code_requested', 'code_delivery_failed', 'code_rejected', 'email_verified',
    'session_created', 'session_revoked'
  )),
  challenge_id uuid,
  session_id uuid,
  ip_hash char(64),
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS starrlabs_auth_audit_email_created_idx
  ON starrlabs_auth_audit(identity_email, created_at DESC);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brawl_prod_app') THEN
    GRANT SELECT, INSERT, UPDATE ON starrlabs_email_login_challenges TO CURRENT_USER;
    GRANT SELECT, INSERT, UPDATE ON starrlabs_identity_sessions TO CURRENT_USER;
    GRANT SELECT, INSERT ON starrlabs_auth_audit TO CURRENT_USER;
    GRANT USAGE, SELECT ON SEQUENCE starrlabs_auth_audit_id_seq TO CURRENT_USER;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brawl_dev_app') THEN
    GRANT SELECT, INSERT, UPDATE ON starrlabs_email_login_challenges TO CURRENT_USER;
    GRANT SELECT, INSERT, UPDATE ON starrlabs_identity_sessions TO CURRENT_USER;
    GRANT SELECT, INSERT ON starrlabs_auth_audit TO CURRENT_USER;
    GRANT USAGE, SELECT ON SEQUENCE starrlabs_auth_audit_id_seq TO CURRENT_USER;
  END IF;
END $$;
