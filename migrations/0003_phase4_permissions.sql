-- Phase 4 application and monitoring permissions.

GRANT SELECT, INSERT, UPDATE, DELETE ON
  public.collector_polls,
  public.tracked_player_state,
  public.player_profile_snapshots,
  public.player_brawler_snapshots,
  public.battle_sources,
  public.official_reference_snapshots
TO CURRENT_USER;

GRANT SELECT ON public.tracked_players TO CURRENT_USER;

GRANT SELECT, INSERT, UPDATE ON public.battles, public.player_profiles, public.player_brawlers
TO CURRENT_USER;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public
TO CURRENT_USER;

GRANT SELECT ON
  public.collector_polls,
  public.tracked_player_state,
  public.player_profile_snapshots,
  public.player_brawler_snapshots,
  public.battle_sources,
  public.official_reference_snapshots,
  public.tracked_players
TO CURRENT_USER;

INSERT INTO public.schema_migrations(version)
VALUES ('0003_phase4_permissions')
ON CONFLICT (version) DO NOTHING;
