"""Aggregate then prune Phase 4 rebuildable/detail data under explicit policy."""

from __future__ import annotations

import argparse

from database import connect


def run(apply: bool) -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            # Build permanent monthly player summaries before detailed expiry.
            cur.execute(
                """
                INSERT INTO player_monthly_progression (
                  player_tag, month, first_snapshot_at, last_snapshot_at,
                  min_trophies, max_trophies, ending_trophies,
                  ending_ranked_elo, ending_ranked_rank, ending_total_prestige_level,
                  snapshot_count
                )
                SELECT DISTINCT ON (player_tag, date_trunc('month', captured_at)::date)
                  player_tag,
                  date_trunc('month', captured_at)::date AS month,
                  min(captured_at) OVER w,
                  max(captured_at) OVER w,
                  min(trophies) OVER w,
                  max(trophies) OVER w,
                  first_value(trophies) OVER w_desc,
                  first_value(ranked_elo) OVER w_desc,
                  first_value(ranked_rank) OVER w_desc,
                  first_value(total_prestige_level) OVER w_desc,
                  count(*) OVER w
                FROM player_profile_snapshots
                WHERE captured_at < now() - interval '24 months'
                WINDOW
                  w AS (PARTITION BY player_tag, date_trunc('month', captured_at)),
                  w_desc AS (
                    PARTITION BY player_tag, date_trunc('month', captured_at)
                    ORDER BY captured_at DESC
                  )
                ORDER BY player_tag, date_trunc('month', captured_at)::date, captured_at DESC
                ON CONFLICT (player_tag, month) DO UPDATE SET
                  last_snapshot_at = EXCLUDED.last_snapshot_at,
                  min_trophies = EXCLUDED.min_trophies,
                  max_trophies = EXCLUDED.max_trophies,
                  ending_trophies = EXCLUDED.ending_trophies,
                  ending_ranked_elo = EXCLUDED.ending_ranked_elo,
                  ending_ranked_rank = EXCLUDED.ending_ranked_rank,
                  ending_total_prestige_level = EXCLUDED.ending_total_prestige_level,
                  snapshot_count = EXCLUDED.snapshot_count
                """
            )
            player_months = cur.rowcount

            cur.execute("SELECT count(*) FROM player_profile_snapshots WHERE captured_at < now() - interval '24 months'")
            old_snapshots = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM collector_polls WHERE started_at < now() - interval '90 days'")
            old_polls = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM battle_features_3v3 WHERE battletime < now() - interval '180 days'")
            old_features = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM pipeline_health_snapshots WHERE captured_at < now() - interval '180 days'")
            old_health = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM starrlabs_email_login_challenges WHERE created_at < now() - interval '30 days'")
            old_auth_challenges = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM starrlabs_identity_sessions "
                "WHERE expires_at < now() - interval '7 days' OR revoked_at < now() - interval '30 days'"
            )
            old_auth_sessions = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM starrlabs_auth_audit WHERE created_at < now() - interval '180 days'")
            old_auth_audit = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM opponent_profile_discoveries WHERE discovered_at < now() - interval '180 days'")
            old_opponent_discoveries = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM opponent_profile_pool WHERE last_discovered_at < now() - interval '180 days'")
            old_opponent_pool = cur.fetchone()[0]

            if apply:
                # Child brawler snapshots cascade with profile snapshot deletion.
                cur.execute("DELETE FROM player_profile_snapshots WHERE captured_at < now() - interval '24 months'")
                cur.execute("DELETE FROM collector_polls WHERE started_at < now() - interval '90 days'")
                cur.execute("DELETE FROM battle_features_3v3 WHERE battletime < now() - interval '180 days'")
                cur.execute("DELETE FROM pipeline_health_snapshots WHERE captured_at < now() - interval '180 days'")
                cur.execute("DELETE FROM starrlabs_email_login_challenges WHERE created_at < now() - interval '30 days'")
                cur.execute(
                    "DELETE FROM starrlabs_identity_sessions "
                    "WHERE expires_at < now() - interval '7 days' OR revoked_at < now() - interval '30 days'"
                )
                cur.execute("DELETE FROM starrlabs_auth_audit WHERE created_at < now() - interval '180 days'")
                cur.execute("DELETE FROM opponent_profile_discoveries WHERE discovered_at < now() - interval '180 days'")
                cur.execute("DELETE FROM opponent_profile_pool WHERE last_discovered_at < now() - interval '180 days'")
                conn.commit()
            else:
                conn.rollback()

        print(
            f"RETENTION_{'APPLIED' if apply else 'DRY_RUN'}|"
            f"monthly_upserts={player_months}|old_snapshots={old_snapshots}|"
            f"old_polls={old_polls}|old_features={old_features}|old_health={old_health}|"
            f"old_auth_challenges={old_auth_challenges}|old_auth_sessions={old_auth_sessions}|"
            f"old_auth_audit={old_auth_audit}|old_opponent_discoveries={old_opponent_discoveries}|"
            f"old_opponent_pool={old_opponent_pool}"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    run(parser.parse_args().apply)
