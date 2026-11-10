"""Cumulative Phase B feature contract for the Loadout Availability challenger.

This module defines the future offline evaluation matrix. It does not train,
score, or write production predictions. Stage N always contains stages 0..N.
"""

STAGE_FEATURES = {
    0: {
        "categorical": ["mode", "map_key", "team_comp_key", "enemy_comp_key"],
        "numeric": [
            "diff_avg_player_total_trophies", "diff_avg_player_level",
            "diff_avg_brawler_power", "diff_avg_brawler_total_trophies",
            "diff_avg_brawler_rank_on_account", "diff_pct_highest_player_brawler",
        ],
    },
    1: {
        "categorical": [],
        "numeric": [
            "participant_coverage", "team_gadget_count", "enemy_gadget_count",
            "team_star_power_count", "enemy_star_power_count", "team_gear_count",
            "enemy_gear_count", "team_hypercharge_count", "enemy_hypercharge_count",
            "team_buffy_count", "enemy_buffy_count", "team_availability_score",
            "enemy_availability_score",
        ],
    },
    2: {
        "categorical": ["focal_item_signature", "team_item_signature", "enemy_item_signature"],
        "numeric": ["item_reference_resolution"],
    },
    3: {
        "categorical": [],
        "numeric": [
            "diff_gadget_count", "diff_star_power_count", "diff_gear_count",
            "diff_hypercharge_count", "diff_buffy_count", "diff_availability_score",
        ],
    },
    4: {
        "categorical": ["brawler_class", "brawler_item_mode_key", "brawler_item_map_key"],
        "numeric": [
            "map_open_ratio", "map_bush_ratio", "map_choke_ratio",
            "map_longest_sightline", "brawler_attack_range", "brawler_speed",
            "range_open_interaction", "speed_choke_interaction",
        ],
    },
}


def cumulative_features(stage: int) -> dict[str, list[str]]:
    if stage not in STAGE_FEATURES:
        raise ValueError(f"unknown loadout stage {stage}")
    result = {"categorical": [], "numeric": []}
    for current in range(stage + 1):
        for kind in result:
            result[kind].extend(STAGE_FEATURES[current][kind])
    return result


# The baseline row is the tracked/focal player. Every loadout row was built
# using profile snapshots whose captured_at <= battle_time.
OFFLINE_DATASET_SQL = """
SELECT bf.*, lf.*
FROM loadout_lobby_features_3v3 lf
JOIN battle_features_3v3 bf
  ON bf.battletime=lf.battle_time
 AND bf.player_tag=lf.observed_from_tag
 AND bf.mode=lf.mode
WHERE lf.feature_version='loadout_availability_v1'
  AND lf.battle_time <= %(cutoff)s
ORDER BY lf.battle_time,lf.observed_from_tag
"""
