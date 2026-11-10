CREATE TABLE IF NOT EXISTS player_verifications (
  player_tag text PRIMARY KEY REFERENCES player_profiles(tag) ON UPDATE CASCADE ON DELETE CASCADE,
  identity_email text NOT NULL,
  status text NOT NULL DEFAULT 'verified' CHECK (status IN ('verified', 'revoked')),
  verified_at timestamptz NOT NULL DEFAULT now(),
  last_verified_at timestamptz NOT NULL DEFAULT now(),
  verification_method text NOT NULL DEFAULT 'ordered_battles_v1',
  revoked_at timestamptz,
  revoked_reason text
);

CREATE UNIQUE INDEX IF NOT EXISTS one_verified_player_per_identity
  ON player_verifications(identity_email) WHERE status = 'verified';

CREATE TABLE IF NOT EXISTS player_verification_challenges (
  id uuid PRIMARY KEY,
  identity_email text NOT NULL,
  player_tag text NOT NULL REFERENCES player_profiles(tag) ON UPDATE CASCADE ON DELETE CASCADE,
  tasks jsonb NOT NULL,
  matches jsonb NOT NULL DEFAULT '[]'::jsonb,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'verified', 'expired', 'cancelled')),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  completed_at timestamptz,
  CHECK (jsonb_typeof(tasks) = 'array'),
  CHECK (jsonb_array_length(tasks) = 2)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_pending_player_verification_per_identity
  ON player_verification_challenges(identity_email) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS player_verification_challenges_expiry_idx
  ON player_verification_challenges(status, expires_at);
