ALTER TABLE player_verification_challenges
  ADD COLUMN IF NOT EXISTS purpose text NOT NULL DEFAULT 'link'
    CHECK (purpose IN ('link', 'transfer')),
  ADD COLUMN IF NOT EXISTS previous_identity_email text,
  ADD COLUMN IF NOT EXISTS last_polled_at timestamptz;

CREATE TABLE IF NOT EXISTS player_account_audit (
  id bigserial PRIMARY KEY,
  player_tag text NOT NULL REFERENCES player_profiles(tag) ON UPDATE CASCADE ON DELETE CASCADE,
  actor_identity_email text NOT NULL,
  action text NOT NULL CHECK (action IN (
    'verified', 'transferred', 'revoked', 'primary_changed', 'account_used'
  )),
  previous_identity_email text,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS player_account_audit_actor_created_idx
  ON player_account_audit(actor_identity_email, created_at DESC);
CREATE INDEX IF NOT EXISTS player_account_audit_player_created_idx
  ON player_account_audit(player_tag, created_at DESC);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brawl_prod_app') THEN
    GRANT SELECT, INSERT ON player_account_audit TO CURRENT_USER;
    GRANT USAGE, SELECT ON SEQUENCE player_account_audit_id_seq TO CURRENT_USER;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='brawl_dev_app') THEN
    GRANT SELECT, INSERT ON player_account_audit TO CURRENT_USER;
    GRANT USAGE, SELECT ON SEQUENCE player_account_audit_id_seq TO CURRENT_USER;
  END IF;
END $$;
