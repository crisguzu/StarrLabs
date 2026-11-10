"""Audit collection health and gate leakage-safe model retraining readiness."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from database import connect


MIN_LOBBIES = 1_000
MIN_DAYS = 30
MIN_FOCAL_PLAYERS = 50
MIN_MATCH_RATE = 0.90
MIN_REFERENCE_COVERAGE = 0.95
MIN_ITEM_BATTLES = 10_000
MIN_ITEM_DAYS = 30
MIN_ITEM_FOCAL_PLAYERS = 100
MIN_ITEM_PARTICIPANT_COVERAGE = 0.70
MIN_ITEM_FULL_LOBBY_COVERAGE = 0.50
MIN_ITEM_REFERENCE_RESOLUTION = 0.95
MAX_ITEM_FEATURE_BUILD_AGE_HOURS = 2
MAX_REFERENCE_OBSERVATION_AGE_HOURS = 30


def _json_default(value):
    if isinstance(value, (datetime, Decimal)):
        return value.isoformat() if isinstance(value, datetime) else float(value)
    raise TypeError(type(value).__name__)


def _metric_observations(metrics: dict, healthy: bool) -> list[dict]:
    """Flatten readiness metrics into stable time-series observations."""
    reciprocal = metrics["reciprocal"]
    reference = metrics["reference"]
    item = metrics["item_model"]
    shadow = metrics["loadout_shadow"]
    item_thresholds = item["thresholds"]
    solo_thresholds = metrics["thresholds"]

    def observation(model_key, name, value, *, unit="count", threshold=None,
                    operator="gte", numerator=None, denominator=None, phase=None):
        passed = None
        if threshold is not None:
            passed = value >= threshold if operator == "gte" else value <= threshold if operator == "lte" else value == threshold
        return {
            "model_key": model_key,
            "metric_name": name,
            "metric_value": float(value),
            "numerator": None if numerator is None else float(numerator),
            "denominator": None if denominator is None else float(denominator),
            "threshold_value": None if threshold is None else float(threshold),
            "threshold_operator": None if threshold is None else operator,
            "threshold_passed": passed,
            "unit": unit,
            "phase": phase,
            "dimensions": {"dataset": "production", "window": "cumulative"},
        }

    observations = [
        observation("solo_showdown_top4", "eligible_lobbies", reciprocal["solo_lobbies"], threshold=solo_thresholds["solo_lobbies"], phase="evidence_collection"),
        observation("solo_showdown_top4", "coverage_days", reciprocal["coverage_days"], unit="days", threshold=solo_thresholds["coverage_days"], phase="evidence_collection"),
        observation("solo_showdown_top4", "focal_players", reciprocal["focal_players"], threshold=solo_thresholds["focal_players"], phase="evidence_collection"),
        observation("solo_showdown_top4", "reciprocal_match_rate", reciprocal["match_rate"], unit="ratio", threshold=solo_thresholds["match_rate"], numerator=reciprocal["matched"], denominator=reciprocal["requests"], phase="evidence_collection"),
        observation("solo_showdown_top4", "reference_coverage", min(float(reference["brawler_confidence"] or 0), float(reference["map_confidence"] or 0)), unit="ratio", threshold=solo_thresholds["reference_coverage"], phase="evidence_collection"),
        observation("loadout_availability_3v3", "eligible_lobbies", item["eligible_lobbies"], threshold=item_thresholds["eligible_lobbies"], phase="phase_a_collection"),
        observation("loadout_availability_3v3", "coverage_days", item["coverage_days"], unit="days", threshold=item_thresholds["coverage_days"], phase="phase_a_collection"),
        observation("loadout_availability_3v3", "focal_players", item["focal_players"], threshold=item_thresholds["focal_players"], phase="phase_a_collection"),
        observation("loadout_availability_3v3", "participant_coverage", item["participant_coverage"], unit="ratio", threshold=item_thresholds["participant_coverage"], numerator=item["covered_participants"], denominator=item["participants"], phase="phase_a_collection"),
        observation("loadout_availability_3v3", "full_lobby_coverage", item["full_lobby_coverage"], unit="ratio", threshold=item_thresholds["full_lobby_coverage"], numerator=item["full_coverage_lobbies"], denominator=item["eligible_lobbies"], phase="phase_a_collection"),
        observation("loadout_availability_3v3", "item_reference_resolution", item["item_reference_resolution"], unit="ratio", threshold=item_thresholds["item_reference_resolution"], numerator=item["item_reference_resolved"], denominator=item["item_reference_total"], phase="phase_c_gate"),
        observation("loadout_availability_3v3", "stage_3_lobbies", item["stage_3_lobbies"], phase="phase_b_features"),
        observation("loadout_availability_3v3", "stage_4_lobbies", item["stage_4_lobbies"], phase="phase_b_features"),
        observation("loadout_availability_3v3", "shadow_active", shadow["active_models"], unit="boolean", threshold=1, operator="eq", phase="phase_e_shadow"),
        observation("loadout_availability_3v3", "shadow_days", shadow["max_shadow_days"], unit="days", threshold=14, phase="phase_e_shadow"),
        observation("loadout_availability_3v3", "shadow_scores", shadow["scores"], phase="phase_e_shadow"),
        observation("loadout_availability_3v3", "shadow_labeled_outcomes", shadow["labeled_outcomes"], phase="phase_e_shadow"),
        observation("platform", "pipeline_healthy", int(healthy), unit="boolean", threshold=1, operator="eq", phase="operations"),
    ]
    return observations


def audit(persist: bool = True) -> dict:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  count(*) FILTER (WHERE started_at >= now() - interval '24 hours') AS polls_24h,
                  count(*) FILTER (WHERE started_at >= now() - interval '24 hours' AND NOT success
                    AND NOT (endpoint = 'opponent_profile' AND http_status = 404)) AS failures_24h,
                  count(*) FILTER (WHERE started_at >= now() - interval '24 hours' AND rate_limited) AS rate_limits_24h,
                  max(completed_at) FILTER (WHERE endpoint = 'battlelog' AND success) AS newest_battlelog_poll,
                  max(completed_at) FILTER (WHERE endpoint LIKE 'brawlapi:%' AND success) AS newest_brawlapi_poll
                FROM collector_polls
                """
            )
            poll = dict(zip([d.name for d in cur.description], cur.fetchone()))

            cur.execute(
                """
                SELECT count(*) AS tracked_players,
                       count(*) FILTER (WHERE possible_gap) AS possible_gap_players,
                       count(*) FILTER (WHERE last_successful_battle_poll_at >= now() - interval '30 minutes') AS fresh_players
                FROM tracked_player_state
                """
            )
            tracked = dict(zip([d.name for d in cur.description], cur.fetchone()))

            cur.execute(
                """
                SELECT count(*) AS requests,
                       count(*) FILTER (WHERE status = 'matched') AS matched,
                       count(*) FILTER (WHERE status = 'expired') AS expired,
                       count(*) FILTER (WHERE status = 'ambiguous') AS ambiguous,
                       count(*) FILTER (WHERE status = 'failed') AS failed,
                       count(*) FILTER (WHERE status IN ('pending', 'missing')) AS open,
                       count(DISTINCT (target_battletime, target_roster_hash)) AS solo_lobbies,
                       count(DISTINCT discovered_from_tag) AS focal_players,
                       min(target_battle_time) AS coverage_started_at,
                       max(target_battle_time) AS newest_target_at
                FROM reciprocal_battlelog_requests
                """
            )
            reciprocal = dict(zip([d.name for d in cur.description], cur.fetchone()))

            cur.execute(
                """
                SELECT count(*) AS pool_players,
                       count(*) FILTER (WHERE status = 'active') AS active_players,
                       count(*) FILTER (WHERE last_successful_profile_poll_at IS NOT NULL) AS snapshotted_players,
                       count(*) FILTER (WHERE status = 'error') AS error_players,
                       count(*) FILTER (WHERE status = 'not_found') AS not_found_players,
                       count(*) FILTER (WHERE next_profile_poll_at <= now()
                         AND last_discovered_at >= now() - interval '30 days') AS due_players,
                       sum(discovery_count) AS discoveries,
                       min(first_discovered_at) AS coverage_started_at,
                       max(last_successful_profile_poll_at) AS newest_snapshot_at
                FROM opponent_profile_pool
                """
            )
            opponent_profiles = dict(zip([d.name for d in cur.description], cur.fetchone()))

            cur.execute(
                """
                SELECT count(*) AS eligible_lobbies,
                       count(*) FILTER (WHERE full_lobby_coverage) AS full_coverage_lobbies,
                       coalesce(sum(participants), 0) AS participants,
                       coalesce(sum(covered_participants), 0) AS covered_participants,
                       count(DISTINCT observed_from_tag) AS focal_players,
                       min(battle_time) AS first_battle_at,
                       max(battle_time) AS newest_battle_at,
                       coalesce(sum(item_reference_total),0) AS item_reference_total,
                       coalesce(sum(item_reference_resolved),0) AS item_reference_resolved,
                       count(*) FILTER (WHERE max_available_stage >= 3) AS stage_3_lobbies,
                       count(*) FILTER (WHERE max_available_stage >= 4) AS stage_4_lobbies
                FROM loadout_lobby_features_3v3
                WHERE feature_version='loadout_availability_v1'
                """
            )
            item_coverage = dict(zip([d.name for d in cur.description], cur.fetchone()))

            cur.execute(
                """
                SELECT id, completed_at, source_cutoff, participant_rows, lobby_rows,
                       stage_counts, metrics,
                       extract(epoch FROM (now()-completed_at))/3600.0 AS age_hours
                FROM loadout_feature_builds
                WHERE status='complete' AND feature_version='loadout_availability_v1'
                ORDER BY completed_at DESC LIMIT 1
                """
            )
            build_row = cur.fetchone()
            item_feature_build = (
                dict(zip([d.name for d in cur.description], build_row)) if build_row else None
            )

            cur.execute(
                """SELECT count(*) FILTER (WHERE r.status='shadow') AS active_models,
                          coalesce(max(extract(epoch FROM (now()-r.shadow_started_at))/86400)
                            FILTER (WHERE r.status='shadow'),0) AS max_shadow_days,
                          count(s.*) FILTER (WHERE r.status='shadow') AS scores,
                          count(s.*) FILTER (WHERE r.status='shadow' AND s.outcome IS NOT NULL) AS labeled_outcomes
                   FROM loadout_model_registry r
                   LEFT JOIN loadout_shadow_scores s ON s.model_registry_id=r.id"""
            )
            loadout_shadow = dict(zip([d.name for d in cur.description], cur.fetchone()))

            cur.execute(
                """
                SELECT
                  (SELECT count(DISTINCT dataset_path) FROM third_party_reference_snapshots
                    WHERE provider = 'brawlapi') AS reference_datasets,
                  (SELECT max(last_observed_at) FROM third_party_reference_snapshots
                    WHERE provider = 'brawlapi') AS reference_observed_at,
                  (SELECT count(*) FROM current_brawler_mechanics) AS brawler_versions,
                  (SELECT avg(coverage_confidence) FROM current_brawler_mechanics) AS brawler_confidence,
                  (SELECT count(*) FROM current_map_geometry) AS map_versions,
                  (SELECT avg(coverage_confidence) FROM current_map_geometry) AS map_confidence,
                  (SELECT max(completed_at) FROM reference_derivation_runs WHERE status = 'complete') AS derivation_completed_at
                """
            )
            reference = dict(zip([d.name for d in cur.description], cur.fetchone()))

            cur.execute("SELECT pg_database_size(current_database())")
            database_bytes = cur.fetchone()[0]

            now = datetime.now(timezone.utc)
            started = reciprocal["coverage_started_at"]
            coverage_days = (now - started).total_seconds() / 86400 if started else 0.0
            requests = reciprocal["requests"] or 0
            match_rate = (reciprocal["matched"] or 0) / requests if requests else 0.0
            brawler_conf = float(reference["brawler_confidence"] or 0)
            map_conf = float(reference["map_confidence"] or 0)
            reference_observed_at = reference["reference_observed_at"]
            reference_age_hours = (
                (now - reference_observed_at).total_seconds() / 3600
                if reference_observed_at else None
            )
            reference["age_hours"] = reference_age_hours
            item_started = opponent_profiles["coverage_started_at"]
            item_days = (now - item_started).total_seconds() / 86400 if item_started else 0.0
            item_participants = int(item_coverage["participants"] or 0)
            item_lobbies = int(item_coverage["eligible_lobbies"] or 0)
            participant_coverage = (
                int(item_coverage["covered_participants"] or 0) / item_participants
                if item_participants else 0.0
            )
            full_lobby_coverage = (
                int(item_coverage["full_coverage_lobbies"] or 0) / item_lobbies
                if item_lobbies else 0.0
            )
            item_reference_total = int(item_coverage["item_reference_total"] or 0)
            item_reference_resolved = int(item_coverage["item_reference_resolved"] or 0)
            item_reference_resolution = (
                item_reference_resolved / item_reference_total if item_reference_total else 1.0
            )

            warnings = []
            if tracked["possible_gap_players"]:
                warnings.append(f"{tracked['possible_gap_players']} tracked player(s) marked possible_gap")

            unhealthy = []
            if poll["failures_24h"]:
                unhealthy.append(f"{poll['failures_24h']} failed poll(s) in 24h")
            if poll["rate_limits_24h"]:
                unhealthy.append(f"{poll['rate_limits_24h']} rate-limited poll(s) in 24h")
            if reciprocal["ambiguous"] or reciprocal["failed"]:
                unhealthy.append("reciprocal queue contains ambiguous or failed requests")
            if reciprocal["open"]:
                warnings.append(f"{reciprocal['open']} reciprocal request(s) still open")
            if reference["reference_datasets"] != 29:
                unhealthy.append(f"expected 29 BrawlAPI datasets, found {reference['reference_datasets']}")
            if reference["brawler_versions"] < 100 or reference["map_versions"] < 1_000:
                unhealthy.append("derived reference coverage is unexpectedly low")
            if reference_age_hours is None:
                unhealthy.append("no successful BrawlAPI reference observation")
            elif reference_age_hours > MAX_REFERENCE_OBSERVATION_AGE_HOURS:
                unhealthy.append(
                    f"BrawlAPI reference observation is stale ({reference_age_hours:.1f}h old)"
                )
            if opponent_profiles["error_players"]:
                warnings.append(f"{opponent_profiles['error_players']} opponent profile(s) in retryable error state")
            if opponent_profiles["due_players"]:
                warnings.append(f"{opponent_profiles['due_players']} opponent profile(s) due within the bounded pool")
            if item_feature_build is None:
                unhealthy.append("no successful loadout feature build")
            elif float(item_feature_build["age_hours"] or 0) > MAX_ITEM_FEATURE_BUILD_AGE_HOURS:
                unhealthy.append("loadout feature build is stale")

            blockers = list(unhealthy)
            if reciprocal["solo_lobbies"] < MIN_LOBBIES:
                blockers.append(f"need {MIN_LOBBIES} reciprocal Solo lobbies; have {reciprocal['solo_lobbies']}")
            if coverage_days < MIN_DAYS:
                blockers.append(f"need {MIN_DAYS} days of post-upgrade coverage; have {coverage_days:.1f}")
            if reciprocal["focal_players"] < MIN_FOCAL_PLAYERS:
                blockers.append(f"need {MIN_FOCAL_PLAYERS} focal players; have {reciprocal['focal_players']}")
            if match_rate < MIN_MATCH_RATE:
                blockers.append(f"need {MIN_MATCH_RATE:.0%} reciprocal match rate; have {match_rate:.1%}")
            if min(brawler_conf, map_conf) < MIN_REFERENCE_COVERAGE:
                blockers.append("reference confidence is below 95%")

            item_blockers = list(unhealthy)
            if item_lobbies < MIN_ITEM_BATTLES:
                item_blockers.append(f"need {MIN_ITEM_BATTLES} eligible 3v3 lobbies; have {item_lobbies}")
            if item_days < MIN_ITEM_DAYS:
                item_blockers.append(f"need {MIN_ITEM_DAYS} days of opponent-profile coverage; have {item_days:.1f}")
            if int(item_coverage["focal_players"] or 0) < MIN_ITEM_FOCAL_PLAYERS:
                item_blockers.append(
                    f"need {MIN_ITEM_FOCAL_PLAYERS} focal players; have {int(item_coverage['focal_players'] or 0)}"
                )
            if participant_coverage < MIN_ITEM_PARTICIPANT_COVERAGE:
                item_blockers.append(
                    f"need {MIN_ITEM_PARTICIPANT_COVERAGE:.0%} participant item coverage; have {participant_coverage:.1%}"
                )
            if full_lobby_coverage < MIN_ITEM_FULL_LOBBY_COVERAGE:
                item_blockers.append(
                    f"need {MIN_ITEM_FULL_LOBBY_COVERAGE:.0%} full-lobby item coverage; have {full_lobby_coverage:.1%}"
                )
            if item_reference_resolution < MIN_ITEM_REFERENCE_RESOLUTION:
                item_blockers.append(
                    f"need {MIN_ITEM_REFERENCE_RESOLUTION:.0%} item-reference resolution; have {item_reference_resolution:.1%}"
                )

            metrics = {
                "polls": poll,
                "tracked": tracked,
                "reciprocal": {**reciprocal, "match_rate": match_rate, "coverage_days": coverage_days},
                "reference": reference,
                "opponent_profiles": opponent_profiles,
                "item_model": {
                    **item_coverage,
                    "coverage_days": item_days,
                    "participant_coverage": participant_coverage,
                    "full_lobby_coverage": full_lobby_coverage,
                    "item_reference_total": item_reference_total,
                    "item_reference_resolved": item_reference_resolved,
                    "item_reference_resolution": item_reference_resolution,
                    "stage_3_lobbies": int(item_coverage["stage_3_lobbies"] or 0),
                    "stage_4_lobbies": int(item_coverage["stage_4_lobbies"] or 0),
                    "latest_feature_build": item_feature_build,
                    "thresholds": {
                        "eligible_lobbies": MIN_ITEM_BATTLES,
                        "coverage_days": MIN_ITEM_DAYS,
                        "focal_players": MIN_ITEM_FOCAL_PLAYERS,
                        "participant_coverage": MIN_ITEM_PARTICIPANT_COVERAGE,
                        "full_lobby_coverage": MIN_ITEM_FULL_LOBBY_COVERAGE,
                        "item_reference_resolution": MIN_ITEM_REFERENCE_RESOLUTION,
                    },
                    "blockers": item_blockers,
                },
                "loadout_shadow": {
                    "active_models": int(loadout_shadow["active_models"] or 0),
                    "max_shadow_days": float(loadout_shadow["max_shadow_days"] or 0),
                    "scores": int(loadout_shadow["scores"] or 0),
                    "labeled_outcomes": int(loadout_shadow["labeled_outcomes"] or 0),
                },
                "database_bytes": database_bytes,
                "thresholds": {
                    "solo_lobbies": MIN_LOBBIES,
                    "coverage_days": MIN_DAYS,
                    "focal_players": MIN_FOCAL_PLAYERS,
                    "match_rate": MIN_MATCH_RATE,
                    "reference_coverage": MIN_REFERENCE_COVERAGE,
                },
            }
            result = {
                "captured_at": now,
                "healthy": not unhealthy,
                "retraining_ready": not blockers,
                "item_model_ready": not item_blockers,
                "metrics": metrics,
                "warnings": warnings,
                "blockers": blockers,
            }
            if persist:
                cur.execute(
                    """
                    INSERT INTO pipeline_health_snapshots
                      (captured_at, healthy, retraining_ready, metrics, warnings, blockers)
                    VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)
                    RETURNING id
                    """,
                    (
                        now, result["healthy"], result["retraining_ready"],
                        json.dumps(metrics, default=_json_default),
                        json.dumps(warnings), json.dumps(blockers),
                    ),
                )
                health_snapshot_id = cur.fetchone()[0]
                observations = _metric_observations(metrics, result["healthy"])
                cur.executemany(
                    """
                    INSERT INTO model_metric_history
                      (captured_at, model_key, metric_name, metric_value,
                       numerator, denominator, threshold_value, threshold_operator,
                       threshold_passed, unit, phase, source_id, dimensions)
                    VALUES
                      (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    [
                        (
                            now, row["model_key"], row["metric_name"], row["metric_value"],
                            row["numerator"], row["denominator"], row["threshold_value"],
                            row["threshold_operator"], row["threshold_passed"], row["unit"],
                            row["phase"], health_snapshot_id, json.dumps(row["dimensions"]),
                        )
                        for row in observations
                    ],
                )
                conn.commit()
            else:
                conn.rollback()
            return result
    finally:
        conn.close()


if __name__ == "__main__":
    result = audit()
    print(json.dumps(result, default=_json_default, sort_keys=True))
    raise SystemExit(0 if result["healthy"] else 1)
