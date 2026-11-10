#!/usr/bin/env python3
"""Build leakage-safe Phase B loadout availability features.

Only snapshots captured at or before a battle are eligible. The target battle
never supplies ownership, and unknown ownership remains explicit missingness.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from database import connect


FEATURE_VERSION = "loadout_availability_v1"
CORE_MODES = ("brawlBall", "gemGrab", "heist", "hotZone", "knockout", "bounty", "wipeout")


PARTICIPANT_SQL = """
WITH source_cutoff AS (SELECT %(cutoff)s::timestamptz AS cutoff),
dedup AS (
  SELECT DISTINCT ON (b.battletime_ts, b.observed_from_tag, b.player_tag)
    b.battletime_ts AS battle_time, b.observed_from_tag, b.player_tag,
    b.team_index, b.mode, b.type AS battle_type, b.map_id,
    b.brawler_id, b.brawler_name, b.result
  FROM battles b, source_cutoff c
  WHERE b.battletime_ts >= (SELECT min(first_discovered_at) FROM opponent_profile_pool)
    AND b.battletime_ts <= c.cutoff
    AND b.mode = ANY(%(modes)s)
    AND b.type = 'ranked'
    AND b.team_index IS NOT NULL
  ORDER BY b.battletime_ts, b.observed_from_tag, b.player_tag, b.last_observed_at DESC NULLS LAST
), eligible AS (
  SELECT battle_time, observed_from_tag,
         max(team_index) FILTER (WHERE player_tag = observed_from_tag) AS focal_team_index,
         max(result) FILTER (WHERE player_tag = observed_from_tag) AS focal_result
  FROM dedup
  GROUP BY battle_time, observed_from_tag
  HAVING count(*) = 6 AND count(DISTINCT team_index) = 2
     AND count(*) FILTER (WHERE team_index=0) = 3
     AND count(*) FILTER (WHERE team_index=1) = 3
     AND count(*) FILTER (WHERE player_tag = observed_from_tag) = 1
), base AS (
  SELECT d.*, e.focal_team_index, e.focal_result,
         CASE e.focal_result WHEN 'victory' THEN 1 WHEN 'defeat' THEN 0 END::smallint AS win_label
  FROM dedup d JOIN eligible e USING (battle_time, observed_from_tag)
), contextual AS (
  SELECT b.*, ps.id AS profile_snapshot_id, ps.captured_at AS snapshot_captured_at,
         bs.star_powers_json, bs.gadgets_json, bs.gears_json,
         bs.hypercharges_json, bs.buffies_json,
         public.reference_derivation_run_at(b.battle_time, b.battle_time) AS reference_run_id
  FROM base b
  LEFT JOIN LATERAL (
    SELECT p.id, p.captured_at
    FROM player_profile_snapshots p
    WHERE p.player_tag=b.player_tag AND p.captured_at <= b.battle_time
      AND p.historical_precision='timestamped'
    ORDER BY p.captured_at DESC, p.id DESC LIMIT 1
  ) ps ON true
  LEFT JOIN player_brawler_snapshots bs
    ON bs.profile_snapshot_id=ps.id AND bs.brawler_id=b.brawler_id
), counted AS (
  SELECT c.*,
    CASE WHEN c.profile_snapshot_id IS NULL OR c.star_powers_json IS NULL THEN NULL ELSE jsonb_array_length(c.star_powers_json) END AS sp_count,
    CASE WHEN c.profile_snapshot_id IS NULL OR c.gadgets_json IS NULL THEN NULL ELSE jsonb_array_length(c.gadgets_json) END AS gadget_count,
    CASE WHEN c.profile_snapshot_id IS NULL OR c.gears_json IS NULL THEN NULL ELSE jsonb_array_length(c.gears_json) END AS gear_count,
    CASE WHEN c.profile_snapshot_id IS NULL OR c.hypercharges_json IS NULL THEN NULL ELSE jsonb_array_length(c.hypercharges_json) END AS hc_count,
    CASE WHEN c.profile_snapshot_id IS NULL OR c.buffies_json IS NULL THEN NULL ELSE
      (SELECT count(*) FROM jsonb_each(c.buffies_json) x WHERE x.value='true'::jsonb) END AS buffy_count,
    coalesce((SELECT count(*) FROM jsonb_array_elements(coalesce(c.star_powers_json,'[]')) x),0)
      + coalesce((SELECT count(*) FROM jsonb_array_elements(coalesce(c.gadgets_json,'[]')) x),0)
      + coalesce((SELECT count(*) FROM jsonb_array_elements(coalesce(c.gears_json,'[]')) x),0)
      + coalesce((SELECT count(*) FROM jsonb_array_elements(coalesce(c.hypercharges_json,'[]')) x),0) AS ref_total,
    coalesce((SELECT count(*) FROM jsonb_array_elements(coalesce(c.star_powers_json,'[]')) x WHERE x ? 'id' AND nullif(x->>'name','') IS NOT NULL),0)
      + coalesce((SELECT count(*) FROM jsonb_array_elements(coalesce(c.gadgets_json,'[]')) x WHERE x ? 'id' AND nullif(x->>'name','') IS NOT NULL),0)
      + coalesce((SELECT count(*) FROM jsonb_array_elements(coalesce(c.gears_json,'[]')) x WHERE x ? 'id' AND nullif(x->>'name','') IS NOT NULL),0)
      + coalesce((SELECT count(*) FROM jsonb_array_elements(coalesce(c.hypercharges_json,'[]')) x WHERE x ? 'id' AND nullif(x->>'name','') IS NOT NULL),0) AS ref_resolved
  FROM contextual c
)
INSERT INTO loadout_participant_features_3v3 (
  battle_time, observed_from_tag, player_tag, team_index, focal_team_index,
  is_focal_player, is_focal_team, mode, battle_type, map_id, brawler_id,
  brawler_name, win_label, profile_snapshot_id, snapshot_captured_at,
  snapshot_age_seconds, has_prebattle_snapshot, gadget_count, star_power_count,
  gear_count, hypercharge_count, buffy_count, has_gadget, has_star_power,
  has_gear, has_hypercharge, availability_score, item_reference_total,
  item_reference_resolved, item_reference_resolution, item_identities,
  item_identity_signature, brawler_class, brawler_attack_range, brawler_speed,
  map_open_ratio, map_bush_ratio, map_choke_ratio, map_longest_sightline,
  reference_confidence, reference_known_at, feature_version, updated_at
)
SELECT c.battle_time,c.observed_from_tag,c.player_tag,c.team_index,c.focal_team_index,
  c.player_tag=c.observed_from_tag,c.team_index=c.focal_team_index,c.mode,c.battle_type,
  c.map_id,c.brawler_id,c.brawler_name,c.win_label,c.profile_snapshot_id,c.snapshot_captured_at,
  CASE WHEN c.snapshot_captured_at IS NULL THEN NULL ELSE extract(epoch FROM (c.battle_time-c.snapshot_captured_at))::bigint END,
  c.profile_snapshot_id IS NOT NULL AND c.sp_count IS NOT NULL,
  c.gadget_count,c.sp_count,c.gear_count,c.hc_count,c.buffy_count,
  CASE WHEN c.gadget_count IS NULL THEN NULL ELSE c.gadget_count>0 END,
  CASE WHEN c.sp_count IS NULL THEN NULL ELSE c.sp_count>0 END,
  CASE WHEN c.gear_count IS NULL THEN NULL ELSE c.gear_count>0 END,
  CASE WHEN c.hc_count IS NULL THEN NULL ELSE c.hc_count>0 END,
  CASE WHEN c.sp_count IS NULL THEN NULL ELSE
    (least(c.gadget_count,1)+least(c.sp_count,1)+least(c.gear_count,2)/2.0+least(c.hc_count,1))/4.0 END,
  c.ref_total,c.ref_resolved,CASE WHEN c.ref_total=0 THEN 1.0 ELSE c.ref_resolved::numeric/c.ref_total END,
  jsonb_build_object('gadgets',coalesce(c.gadgets_json,'[]'),'star_powers',coalesce(c.star_powers_json,'[]'),
    'gears',coalesce(c.gears_json,'[]'),'hypercharges',coalesce(c.hypercharges_json,'[]'),'buffies',coalesce(c.buffies_json,'{}')),
  CASE WHEN c.profile_snapshot_id IS NULL THEN NULL ELSE md5(concat_ws('|',
    (SELECT string_agg(x->>'id',',' ORDER BY x->>'id') FROM jsonb_array_elements(coalesce(c.gadgets_json,'[]')) x),
    (SELECT string_agg(x->>'id',',' ORDER BY x->>'id') FROM jsonb_array_elements(coalesce(c.star_powers_json,'[]')) x),
    (SELECT string_agg(x->>'id',',' ORDER BY x->>'id') FROM jsonb_array_elements(coalesce(c.gears_json,'[]')) x),
    (SELECT string_agg(x->>'id',',' ORDER BY x->>'id') FROM jsonb_array_elements(coalesce(c.hypercharges_json,'[]')) x))) END,
  bm.class_archetype,bm.attack_range,bm.speed,mg.open_ratio,mg.bush_ratio,mg.choke_ratio,mg.longest_sightline,
  least(bm.coverage_confidence,mg.coverage_confidence),rr.known_at,%(version)s,now()
FROM counted c
LEFT JOIN reference_derivation_runs rr ON rr.id=c.reference_run_id
LEFT JOIN brawler_mechanics_versions bm ON bm.derivation_run_id=c.reference_run_id AND bm.brawler_id=c.brawler_id
LEFT JOIN map_geometry_versions mg ON mg.derivation_run_id=c.reference_run_id AND mg.location_id=c.map_id
ON CONFLICT (battle_time,observed_from_tag,player_tag) DO UPDATE SET
  profile_snapshot_id=excluded.profile_snapshot_id,snapshot_captured_at=excluded.snapshot_captured_at,
  snapshot_age_seconds=excluded.snapshot_age_seconds,has_prebattle_snapshot=excluded.has_prebattle_snapshot,
  gadget_count=excluded.gadget_count,star_power_count=excluded.star_power_count,gear_count=excluded.gear_count,
  hypercharge_count=excluded.hypercharge_count,buffy_count=excluded.buffy_count,has_gadget=excluded.has_gadget,
  has_star_power=excluded.has_star_power,has_gear=excluded.has_gear,has_hypercharge=excluded.has_hypercharge,
  availability_score=excluded.availability_score,item_reference_total=excluded.item_reference_total,
  item_reference_resolved=excluded.item_reference_resolved,item_reference_resolution=excluded.item_reference_resolution,
  item_identities=excluded.item_identities,item_identity_signature=excluded.item_identity_signature,
  brawler_class=excluded.brawler_class,brawler_attack_range=excluded.brawler_attack_range,brawler_speed=excluded.brawler_speed,
  map_open_ratio=excluded.map_open_ratio,map_bush_ratio=excluded.map_bush_ratio,map_choke_ratio=excluded.map_choke_ratio,
  map_longest_sightline=excluded.map_longest_sightline,reference_confidence=excluded.reference_confidence,
  reference_known_at=excluded.reference_known_at,feature_version=excluded.feature_version,updated_at=now()
"""


LOBBY_SQL = """
INSERT INTO loadout_lobby_features_3v3 (
  battle_time,observed_from_tag,mode,battle_type,map_id,focal_team_index,win_label,
  participants,covered_participants,full_lobby_coverage,participant_coverage,
  item_reference_total,item_reference_resolved,item_reference_resolution,
  team_gadget_count,enemy_gadget_count,diff_gadget_count,
  team_star_power_count,enemy_star_power_count,diff_star_power_count,
  team_gear_count,enemy_gear_count,diff_gear_count,
  team_hypercharge_count,enemy_hypercharge_count,diff_hypercharge_count,
  team_buffy_count,enemy_buffy_count,diff_buffy_count,
  team_availability_score,enemy_availability_score,diff_availability_score,
  team_item_signature,enemy_item_signature,focal_brawler_id,focal_item_signature,
  brawler_class,map_open_ratio,map_bush_ratio,map_choke_ratio,map_longest_sightline,
  brawler_attack_range,brawler_speed,range_open_interaction,speed_choke_interaction,
  brawler_item_mode_key,brawler_item_map_key,max_available_stage,feature_version,updated_at
)
SELECT battle_time,observed_from_tag,max(mode),max(battle_type),max(map_id),max(focal_team_index),max(win_label),
  count(*),count(*) FILTER (WHERE has_prebattle_snapshot),bool_and(has_prebattle_snapshot),
  count(*) FILTER (WHERE has_prebattle_snapshot)::numeric/count(*),sum(item_reference_total),sum(item_reference_resolved),
  CASE WHEN sum(item_reference_total)=0 THEN 1.0 ELSE sum(item_reference_resolved)::numeric/sum(item_reference_total) END,
  sum(coalesce(gadget_count,0)) FILTER (WHERE is_focal_team),sum(coalesce(gadget_count,0)) FILTER (WHERE NOT is_focal_team),
  sum(coalesce(gadget_count,0)) FILTER (WHERE is_focal_team)-sum(coalesce(gadget_count,0)) FILTER (WHERE NOT is_focal_team),
  sum(coalesce(star_power_count,0)) FILTER (WHERE is_focal_team),sum(coalesce(star_power_count,0)) FILTER (WHERE NOT is_focal_team),
  sum(coalesce(star_power_count,0)) FILTER (WHERE is_focal_team)-sum(coalesce(star_power_count,0)) FILTER (WHERE NOT is_focal_team),
  sum(coalesce(gear_count,0)) FILTER (WHERE is_focal_team),sum(coalesce(gear_count,0)) FILTER (WHERE NOT is_focal_team),
  sum(coalesce(gear_count,0)) FILTER (WHERE is_focal_team)-sum(coalesce(gear_count,0)) FILTER (WHERE NOT is_focal_team),
  sum(coalesce(hypercharge_count,0)) FILTER (WHERE is_focal_team),sum(coalesce(hypercharge_count,0)) FILTER (WHERE NOT is_focal_team),
  sum(coalesce(hypercharge_count,0)) FILTER (WHERE is_focal_team)-sum(coalesce(hypercharge_count,0)) FILTER (WHERE NOT is_focal_team),
  sum(coalesce(buffy_count,0)) FILTER (WHERE is_focal_team),sum(coalesce(buffy_count,0)) FILTER (WHERE NOT is_focal_team),
  sum(coalesce(buffy_count,0)) FILTER (WHERE is_focal_team)-sum(coalesce(buffy_count,0)) FILTER (WHERE NOT is_focal_team),
  avg(availability_score) FILTER (WHERE is_focal_team),avg(availability_score) FILTER (WHERE NOT is_focal_team),
  avg(availability_score) FILTER (WHERE is_focal_team)-avg(availability_score) FILTER (WHERE NOT is_focal_team),
  md5(string_agg(coalesce(item_identity_signature,'missing'),',' ORDER BY player_tag) FILTER (WHERE is_focal_team)),
  md5(string_agg(coalesce(item_identity_signature,'missing'),',' ORDER BY player_tag) FILTER (WHERE NOT is_focal_team)),
  max(brawler_id) FILTER (WHERE is_focal_player),max(item_identity_signature) FILTER (WHERE is_focal_player),
  max(brawler_class) FILTER (WHERE is_focal_player),max(map_open_ratio),max(map_bush_ratio),max(map_choke_ratio),max(map_longest_sightline),
  max(brawler_attack_range) FILTER (WHERE is_focal_player),max(brawler_speed) FILTER (WHERE is_focal_player),
  max(brawler_attack_range) FILTER (WHERE is_focal_player)*max(map_open_ratio),
  max(brawler_speed) FILTER (WHERE is_focal_player)*max(map_choke_ratio),
  concat_ws('|',max(brawler_id) FILTER (WHERE is_focal_player),max(item_identity_signature) FILTER (WHERE is_focal_player),max(mode)),
  concat_ws('|',max(brawler_id) FILTER (WHERE is_focal_player),max(item_identity_signature) FILTER (WHERE is_focal_player),max(map_id)),
  CASE WHEN bool_and(has_prebattle_snapshot) AND bool_and(reference_confidence IS NOT NULL) THEN 4
       WHEN bool_and(has_prebattle_snapshot) THEN 3
       WHEN count(*) FILTER (WHERE has_prebattle_snapshot)>0 THEN 2 ELSE 0 END,
  %(version)s,now()
FROM loadout_participant_features_3v3
WHERE battle_time <= %(cutoff)s AND feature_version=%(version)s
GROUP BY battle_time,observed_from_tag
HAVING count(*)=6
ON CONFLICT (battle_time,observed_from_tag) DO UPDATE SET
  participants=excluded.participants,covered_participants=excluded.covered_participants,
  full_lobby_coverage=excluded.full_lobby_coverage,participant_coverage=excluded.participant_coverage,
  item_reference_total=excluded.item_reference_total,item_reference_resolved=excluded.item_reference_resolved,
  item_reference_resolution=excluded.item_reference_resolution,team_gadget_count=excluded.team_gadget_count,
  enemy_gadget_count=excluded.enemy_gadget_count,diff_gadget_count=excluded.diff_gadget_count,
  team_star_power_count=excluded.team_star_power_count,enemy_star_power_count=excluded.enemy_star_power_count,
  diff_star_power_count=excluded.diff_star_power_count,team_gear_count=excluded.team_gear_count,
  enemy_gear_count=excluded.enemy_gear_count,diff_gear_count=excluded.diff_gear_count,
  team_hypercharge_count=excluded.team_hypercharge_count,enemy_hypercharge_count=excluded.enemy_hypercharge_count,
  diff_hypercharge_count=excluded.diff_hypercharge_count,team_buffy_count=excluded.team_buffy_count,
  enemy_buffy_count=excluded.enemy_buffy_count,diff_buffy_count=excluded.diff_buffy_count,
  team_availability_score=excluded.team_availability_score,enemy_availability_score=excluded.enemy_availability_score,
  diff_availability_score=excluded.diff_availability_score,team_item_signature=excluded.team_item_signature,
  enemy_item_signature=excluded.enemy_item_signature,focal_brawler_id=excluded.focal_brawler_id,
  focal_item_signature=excluded.focal_item_signature,brawler_class=excluded.brawler_class,
  map_open_ratio=excluded.map_open_ratio,map_bush_ratio=excluded.map_bush_ratio,map_choke_ratio=excluded.map_choke_ratio,
  map_longest_sightline=excluded.map_longest_sightline,brawler_attack_range=excluded.brawler_attack_range,
  brawler_speed=excluded.brawler_speed,range_open_interaction=excluded.range_open_interaction,
  speed_choke_interaction=excluded.speed_choke_interaction,brawler_item_mode_key=excluded.brawler_item_mode_key,
  brawler_item_map_key=excluded.brawler_item_map_key,max_available_stage=excluded.max_available_stage,
  feature_version=excluded.feature_version,updated_at=now()
"""


def refresh() -> dict:
    cutoff = datetime.now(timezone.utc)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO loadout_feature_builds(status,feature_version,source_cutoff) VALUES ('running',%s,%s) RETURNING id",
                (FEATURE_VERSION, cutoff),
            )
            build_id = cur.fetchone()[0]
        conn.commit()
        try:
            with conn.cursor() as cur:
                params = {"cutoff": cutoff, "modes": list(CORE_MODES), "version": FEATURE_VERSION}
                cur.execute(PARTICIPANT_SQL, params)
                cur.execute(LOBBY_SQL, params)
                cur.execute("SELECT count(*) FROM loadout_participant_features_3v3 WHERE feature_version=%s", (FEATURE_VERSION,))
                participants = cur.fetchone()[0]
                cur.execute("SELECT count(*),coalesce(jsonb_object_agg(max_available_stage,n), '{}'::jsonb) FROM (SELECT max_available_stage,count(*) n FROM loadout_lobby_features_3v3 WHERE feature_version=%s GROUP BY max_available_stage) x", (FEATURE_VERSION,))
                stage_groups, stage_counts = cur.fetchone()
                cur.execute("SELECT count(*),coalesce(avg(participant_coverage),0),coalesce(sum(item_reference_resolved)::numeric/nullif(sum(item_reference_total),0),1) FROM loadout_lobby_features_3v3 WHERE feature_version=%s", (FEATURE_VERSION,))
                lobbies, coverage, resolution = cur.fetchone()
                metrics = {"mean_participant_coverage": float(coverage), "item_reference_resolution": float(resolution)}
                cur.execute("UPDATE loadout_feature_builds SET status='complete',completed_at=now(),participant_rows=%s,lobby_rows=%s,stage_counts=%s::jsonb,metrics=%s::jsonb WHERE id=%s", (participants,lobbies,json.dumps(stage_counts),json.dumps(metrics),build_id))
            return {"build_id": build_id, "feature_version": FEATURE_VERSION, "source_cutoff": cutoff.isoformat(), "participant_rows": participants, "lobby_rows": lobbies, "stage_counts": stage_counts, **metrics}
        except Exception as exc:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("UPDATE loadout_feature_builds SET status='failed',completed_at=now(),error_message=%s WHERE id=%s", (str(exc)[:2000],build_id))
            conn.commit()
            raise


if __name__ == "__main__":
    print("LOADOUT_FEATURE_BUILD|" + json.dumps(refresh(), sort_keys=True))
