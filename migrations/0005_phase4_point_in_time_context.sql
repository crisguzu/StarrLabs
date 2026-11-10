-- Safe point-in-time profile context for model feature generation.
-- Consumers must filter by battle/player; the lateral lookup uses the
-- (player_tag, captured_at DESC) snapshot index.

CREATE OR REPLACE VIEW public.battle_profile_context AS
SELECT
  b.battletime,
  b.battletime_ts,
  b.player_tag,
  b.mode,
  b.map_id,
  b.brawler_id,
  s.id AS profile_snapshot_id,
  s.captured_at AS profile_captured_at,
  CASE
    WHEN s.captured_at IS NULL OR b.battletime_ts IS NULL THEN NULL
    ELSE extract(epoch FROM (b.battletime_ts - s.captured_at))::bigint
  END AS profile_age_at_battle_seconds,
  s.trophies AS profile_trophies,
  s.ranked_elo,
  s.ranked_rank,
  s.total_prestige_level,
  bs.power AS snapshot_brawler_power,
  bs.trophies AS snapshot_brawler_trophies,
  bs.prestige_level AS snapshot_brawler_prestige_level,
  bs.current_win_streak AS snapshot_brawler_win_streak
FROM public.battles b
LEFT JOIN LATERAL (
  SELECT ps.*
  FROM public.player_profile_snapshots ps
  WHERE ps.player_tag = b.player_tag
    AND ps.captured_at <= b.battletime_ts
    AND ps.historical_precision = 'timestamped'
  ORDER BY ps.captured_at DESC
  LIMIT 1
) s ON true
LEFT JOIN public.player_brawler_snapshots bs
  ON bs.profile_snapshot_id = s.id
 AND bs.brawler_id = b.brawler_id;

GRANT SELECT ON public.battle_profile_context
TO CURRENT_USER;

INSERT INTO public.schema_migrations(version)
VALUES ('0005_phase4_point_in_time_context')
ON CONFLICT (version) DO NOTHING;
