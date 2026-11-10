"""Explainable post-battle bot-control detection.

Account resolution is deliberately separate from control classification: Supercell
can resolve accounts that were controlled by bots in a particular battle.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List


LOBBY_LABELS = {"bots", "humans", "mixed", "unsure"}


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def participant_evidence(row: Dict[str, Any], scout_result: str | None = None) -> Dict[str, Any]:
    tag = str(row.get("player_tag") or "").upper()
    name = str(row.get("player_name") or "").strip()
    profile_captured = bool(row.get("profile_captured_at") or row.get("current_profile_captured_at"))
    account_found = profile_captured or scout_result in {"cached", "fetched"}
    profile_not_found = scout_result == "not_found" and not account_found
    profile_unavailable = scout_result in {"unavailable", "error", "rate_limited"} and not account_found

    total_trophies = _integer(row.get("current_profile_trophies"))
    if total_trophies is None:
        total_trophies = _integer(row.get("snapshot_profile_trophies"))
    highest_trophies = _integer(row.get("current_profile_highest_trophies"))
    if highest_trophies is None:
        highest_trophies = _integer(row.get("snapshot_profile_highest_trophies"))
    exp_level = _integer(row.get("current_profile_exp_level"))
    if exp_level is None:
        exp_level = _integer(row.get("snapshot_profile_exp_level"))
    victories_3v3 = _integer(row.get("current_victories_3v3"))
    solo_victories = _integer(row.get("current_solo_victories"))
    duo_victories = _integer(row.get("current_duo_victories"))
    owned_brawlers = _integer(row.get("current_owned_brawlers"))
    battle_brawler_trophies = _integer(row.get("brawler_trophies"))

    reasons: List[str] = []
    hard_bot = False
    if account_found:
        zero_victories = all(v in {0, None} for v in (victories_3v3, solo_victories, duo_victories))
        impossible_profile = (
            total_trophies == 0
            and highest_trophies == 0
            and exp_level == 1
            and zero_victories
            and owned_brawlers == 1
            and battle_brawler_trophies is not None
            and battle_brawler_trophies >= 500
        )
        if impossible_profile:
            hard_bot = True
            reasons.append("synthetic_profile_trophy_contradiction")

    tag_body = tag.lstrip("#")
    explicit_bot_name = bool(re.fullmatch(r"Bot\s+\d+", name, flags=re.IGNORECASE))
    short_synthetic_tag = 0 < len(tag_body) <= 3
    if explicit_bot_name and short_synthetic_tag and profile_not_found:
        hard_bot = True
        reasons.append("explicit_bot_identity_no_profile")

    if hard_bot:
        control_kind, control_label = "bot", "Bot"
    elif profile_not_found and (explicit_bot_name or short_synthetic_tag):
        control_kind, control_label = "likely-bot", "Likely bot"
        reasons.append("synthetic_identity_profile_unavailable")
    else:
        control_kind, control_label = "unknown", "Unknown"
        reasons.append("no_deterministic_control_evidence")

    return {
        "account_kind": "found" if account_found else "not-found" if profile_not_found else "unavailable",
        "account_label": "Account found" if account_found else "Account not found" if profile_not_found else "Account temporarily unavailable",
        "control_kind": control_kind,
        "control_label": control_label,
        "evidence_reasons": reasons,
        "hard_bot_signal": hard_bot,
        "features": {
            "profile_resolved": account_found,
            "profile_not_found": profile_not_found,
            "profile_unavailable": profile_unavailable,
            "profile_fetch_status": scout_result,
            "total_trophies": total_trophies,
            "highest_trophies": highest_trophies,
            "exp_level": exp_level,
            "victories_3v3": victories_3v3,
            "solo_victories": solo_victories,
            "duo_victories": duo_victories,
            "owned_brawlers": owned_brawlers,
            "battle_brawler_trophies": battle_brawler_trophies,
            "explicit_bot_name": explicit_bot_name,
            "short_synthetic_tag": short_synthetic_tag,
        },
    }


def lobby_detection(participants: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = list(participants)
    bots = sum(bool(row.get("hard_bot_signal")) for row in rows)
    likely = sum(row.get("control_kind") == "likely-bot" for row in rows)
    if rows and bots == len(rows):
        label = "bots"
    elif bots or likely:
        label = "mixed" if bots + likely < len(rows) else "bots"
    else:
        label = "unsure"
    return {
        "label": label,
        "bot_signal_count": bots,
        "likely_bot_signal_count": likely,
        "participant_count": len(rows),
        "reason": "deterministic_post_battle_rules",
    }
