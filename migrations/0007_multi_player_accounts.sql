DROP INDEX IF EXISTS one_verified_player_per_identity;

ALTER TABLE player_verifications
  ADD COLUMN IF NOT EXISTS is_primary boolean NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS last_used_at timestamptz;

WITH ranked AS (
  SELECT player_tag,
         row_number() OVER (PARTITION BY identity_email ORDER BY last_verified_at DESC, player_tag) AS position
  FROM player_verifications
  WHERE status = 'verified'
)
UPDATE player_verifications verification
SET is_primary = (ranked.position = 1),
    last_used_at = COALESCE(verification.last_used_at, verification.last_verified_at)
FROM ranked
WHERE verification.player_tag = ranked.player_tag;

CREATE UNIQUE INDEX IF NOT EXISTS one_primary_verified_player_per_identity
  ON player_verifications(identity_email) WHERE status = 'verified' AND is_primary;

CREATE INDEX IF NOT EXISTS verified_players_by_identity
  ON player_verifications(identity_email, status, last_used_at DESC);

ALTER TABLE player_lists
  ADD COLUMN IF NOT EXISTS identity_email text;

CREATE UNIQUE INDEX IF NOT EXISTS one_my_accounts_list_per_identity
  ON player_lists(identity_email)
  WHERE system_key = 'my_accounts' AND is_deleted = FALSE;
