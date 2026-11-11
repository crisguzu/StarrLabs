-- One row per player-perspective battle; draws excluded only from win-rate denominator.
SELECT json_build_object(
 'observations', (SELECT count(*) FROM public.battlelog_payload_observations),
 'polls', (SELECT count(*) FROM public.collector_polls),
 'raw_payloads', (SELECT count(*) FROM public.battlelog_payloads),
 'battles', count(*),
 'decisive', count(*) FILTER (WHERE b.result IN ('victory','defeat')),
 'wins', count(*) FILTER (WHERE b.result='victory'),
 'context_rows', count(c.profile_snapshot_id),
 'win_rate', round(count(*) FILTER (WHERE b.result='victory')::numeric /
    NULLIF(count(*) FILTER (WHERE b.result IN ('victory','defeat')),0),6),
 'context_coverage', round(count(c.profile_snapshot_id)::numeric/NULLIF(count(*),0),6)
) FROM public.battles b JOIN public.battle_profile_context c
 ON c.battletime=b.battletime AND c.player_tag=b.player_tag;
