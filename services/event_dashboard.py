"""Deterministic analytics for StarrLabs' five-stage limited event.

The official API does not expose a challenge/run identifier.  We therefore
recognize a run from the event's ordered map sequence and its one-use-brawler
rule.  Normal rotation battles on the same maps are ignored unless they form a
valid sequence beginning on floor one.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Mapping


EVENT_BRAWLER_LIMIT = 8
EVENT_STAGES_20260807 = (
    {"number": 1, "mode": "Gem Grab", "mode_key": "gemGrab", "map": "Hard Rock Mine"},
    {"number": 2, "mode": "Brawl Ball", "mode_key": "brawlBall", "map": "Triple Dribble"},
    {"number": 3, "mode": "Bounty", "mode_key": "bounty", "map": "Crystal Clearing"},
    {"number": 4, "mode": "Hot Zone", "mode_key": "hotZone", "map": "Massive Attack"},
    {"number": 5, "mode": "Knockout", "mode_key": "knockout", "map": "Split Difference"},
)
EVENT_STAGES_20260808 = (
    {"number": 1, "mode": "Gem Grab", "mode_key": "gemGrab", "map": "Crystal Arcade"},
    {"number": 2, "mode": "Brawl Ball", "mode_key": "brawlBall", "map": "Sneaky Fields"},
    {"number": 3, "mode": "Bounty", "mode_key": "bounty", "map": "Deathcap Trap"},
    {"number": 4, "mode": "Hot Zone", "mode_key": "hotZone", "map": "Controller Chaos"},
    {"number": 5, "mode": "Knockout", "mode_key": "knockout", "map": "Belle's Rock"},
)
EVENT_STAGES_20260809 = (
    {"number": 1, "mode": "Gem Grab", "mode_key": "gemGrab", "map": "Afterparty"},
    {"number": 2, "mode": "Brawl Ball", "mode_key": "brawlBall", "map": "Pool Party"},
    {"number": 3, "mode": "Bounty", "mode_key": "bounty", "map": "Crystal Clearing"},
    {"number": 4, "mode": "Hot Zone", "mode_key": "hotZone", "map": "Playmaker"},
    {"number": 5, "mode": "Knockout", "mode_key": "knockout", "map": "Konnakol"},
)
EVENT_STAGES_20260813 = (
    {"number": 1, "mode": "Gem Grab", "mode_key": "gemGrab", "map": "Hard Rock Mine"},
    {"number": 2, "mode": "Brawl Ball", "mode_key": "brawlBall", "map": "Bustling Business"},
    {"number": 3, "mode": "Bounty", "mode_key": "bounty", "map": "Tasty Berry"},
    {"number": 4, "mode": "Hot Zone", "mode_key": "hotZone", "map": "Dueling Beetles"},
    {"number": 5, "mode": "Knockout", "mode_key": "knockout", "map": "Flowing Springs"},
)
EVENT_STAGES_BY_DATE = {
    date(2026, 8, 7): EVENT_STAGES_20260807,
    date(2026, 8, 8): EVENT_STAGES_20260808,
    date(2026, 8, 9): EVENT_STAGES_20260809,
    date(2026, 8, 13): EVENT_STAGES_20260813,
}

# Preserve the original sequence as the library default for callers that do
# not select a calendar day explicitly.
EVENT_STAGES = EVENT_STAGES_20260807
EVENT_MAPS = tuple(stage["map"] for stage in EVENT_STAGES)


def event_stages_for_date(selected_date: date) -> tuple[dict[str, Any], ...]:
    """Return the verified floor sequence for a supported event date."""
    return EVENT_STAGES_BY_DATE.get(selected_date, EVENT_STAGES)


def _value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    return getattr(row, key, default)


def _time(row: Any) -> datetime:
    value = _value(row, "battletime_ts")
    if not isinstance(value, datetime):
        raise TypeError("event battle rows require battletime_ts datetime values")
    return value


def _normalize_result(value: Any) -> str:
    result = str(value or "").strip().lower()
    return result if result in {"victory", "defeat", "draw"} else "unknown"


@dataclass(frozen=True)
class CandidateRun:
    attempts: tuple[dict[str, Any], ...]
    cleared_stages: int
    started_at: datetime
    stage_count: int

    @property
    def completed(self) -> bool:
        return self.cleared_stages == self.stage_count

    @property
    def score(self) -> tuple[int, int, int, float]:
        reached = max((attempt["stage"] for attempt in self.attempts), default=0)
        # A coherent five-floor path wins first, then depth and evidence.  A
        # later start breaks exact ties to avoid swallowing unrelated matches.
        return (int(self.completed), reached, len(self.attempts), self.started_at.timestamp())


def _candidate_from(
    rows: list[Any],
    start_index: int,
    event_stages: tuple[dict[str, Any], ...],
) -> CandidateRun:
    expected = 0
    used_brawlers: set[str] = set()
    attempts: list[dict[str, Any]] = []

    for row in rows[start_index:]:
        if expected >= len(event_stages):
            break
        stage = event_stages[expected]
        if _value(row, "map") != stage["map"]:
            continue
        brawler = str(_value(row, "brawler_name") or "Unknown").strip().upper()
        if not brawler or brawler in used_brawlers:
            continue
        result = _normalize_result(_value(row, "result"))
        if result == "unknown":
            continue

        used_brawlers.add(brawler)
        attempts.append(
            {
                "stage": stage["number"],
                "map": stage["map"],
                "mode": stage["mode"],
                "result": result,
                "brawler": brawler,
                "time": _time(row),
            }
        )
        if result == "victory":
            expected += 1

    return CandidateRun(tuple(attempts), expected, _time(rows[start_index]), len(event_stages))


def recognize_player_run(
    rows: Iterable[Any],
    *,
    activity_start: datetime | None = None,
    activity_end: datetime | None = None,
    event_stages: tuple[dict[str, Any], ...] = EVENT_STAGES,
) -> CandidateRun | None:
    """Return the strongest valid event run found in one player's rows.

    When an activity window is provided, completed runs from an earlier day do
    not mask a new daily run. A cross-midnight run remains eligible as long as
    one of its accepted attempts falls inside the requested window.
    """
    ordered = sorted(rows, key=_time)
    starts = [index for index, row in enumerate(ordered) if _value(row, "map") == event_stages[0]["map"]]
    candidates = [_candidate_from(ordered, index, event_stages) for index in starts]
    candidates = [candidate for candidate in candidates if candidate.attempts]
    if activity_start is not None and activity_end is not None:
        candidates = [
            candidate
            for candidate in candidates
            if any(activity_start <= attempt["time"] < activity_end for attempt in candidate.attempts)
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda candidate: (
                int(activity_start <= candidate.started_at < activity_end),
                candidate.score,
            ),
        )
    return max(candidates, key=lambda candidate: candidate.score) if candidates else None


def _percent(numerator: int, denominator: int) -> float | None:
    return round(100 * numerator / denominator, 1) if denominator else None


def build_event_dashboard(
    rows: Iterable[Any],
    *,
    day_start: datetime,
    day_end: datetime,
    map_metadata: Mapping[str, Mapping[str, Any]] | None = None,
    brawler_metadata: Mapping[str, Mapping[str, Any]] | None = None,
    event_stages: tuple[dict[str, Any], ...] = EVENT_STAGES,
) -> dict[str, Any]:
    """Build public dashboard metrics for runs with accepted activity today."""
    grouped: dict[str, list[Any]] = defaultdict(list)
    names: dict[str, str] = {}
    for row in rows:
        tag = str(_value(row, "player_tag") or "").strip().upper()
        if not tag:
            continue
        grouped[tag].append(row)
        name = str(_value(row, "player_name") or "").strip()
        if name:
            names[tag] = name

    players: list[dict[str, Any]] = []
    stage_attempts: dict[int, list[dict[str, Any]]] = defaultdict(list)
    latest_observation: datetime | None = None

    for tag, player_rows in grouped.items():
        run = recognize_player_run(
            player_rows,
            activity_start=day_start,
            activity_end=day_end,
            event_stages=event_stages,
        )
        if run is None:
            continue
        today_attempts = [attempt for attempt in run.attempts if day_start <= attempt["time"] < day_end]
        if not today_attempts:
            continue

        for attempt in today_attempts:
            stage_attempts[attempt["stage"]].append({**attempt, "player_tag": tag})
        latest = max(attempt["time"] for attempt in today_attempts)
        latest_observation = max(latest_observation, latest) if latest_observation else latest
        victories = sum(attempt["result"] == "victory" for attempt in run.attempts)
        defeats = sum(attempt["result"] == "defeat" for attempt in run.attempts)
        draws = sum(attempt["result"] == "draw" for attempt in run.attempts)
        attempts_used = len(run.attempts)
        reached = max(attempt["stage"] for attempt in run.attempts)
        completed = run.completed
        eliminated = not completed and attempts_used >= EVENT_BRAWLER_LIMIT
        current_stage = None if completed or eliminated else min(run.cleared_stages + 1, len(event_stages))
        state = "Complete" if completed else ("Out of picks" if eliminated else f"Floor {current_stage}")

        players.append(
            {
                "tag": tag,
                "tag_path": tag.lstrip("#"),
                "name": names.get(tag, tag),
                "attempts": attempts_used,
                "today_attempts": len(today_attempts),
                "wins": victories,
                "losses": defeats,
                "draws": draws,
                "win_rate": _percent(victories, attempts_used),
                "cleared_stages": run.cleared_stages,
                "reached_stage": reached,
                "current_stage": current_stage,
                "remaining_picks": max(EVENT_BRAWLER_LIMIT - attempts_used, 0),
                "completed": completed,
                "perfect": completed and defeats == 0 and draws == 0,
                "eliminated": eliminated,
                "state": state,
                "latest": latest,
                "started_today": day_start <= run.started_at < day_end,
                "attempt_log": list(run.attempts),
            }
        )

    players.sort(
        key=lambda player: (
            not player["completed"],
            -player["cleared_stages"],
            player["attempts"],
            -player["latest"].timestamp(),
        )
    )

    stage_cards: list[dict[str, Any]] = []
    for stage in event_stages:
        stage_number = stage["number"]
        attempts = stage_attempts[stage_number]
        wins = sum(attempt["result"] == "victory" for attempt in attempts)
        losses = sum(attempt["result"] == "defeat" for attempt in attempts)
        draws = sum(attempt["result"] == "draw" for attempt in attempts)
        entrants = {attempt["player_tag"] for attempt in attempts}
        cleared_players = sum(player["cleared_stages"] >= stage_number for player in players)
        reached_players = sum(player["reached_stage"] >= stage_number for player in players)
        stuck_players = sum(
            not player["completed"] and player["current_stage"] == stage_number for player in players
        )
        brawlers: dict[str, dict[str, int]] = defaultdict(lambda: {"attempts": 0, "wins": 0})
        for attempt in attempts:
            brawlers[attempt["brawler"]]["attempts"] += 1
            brawlers[attempt["brawler"]]["wins"] += int(attempt["result"] == "victory")
        brawler_rows = []
        for name, values in brawlers.items():
            metadata = dict((brawler_metadata or {}).get(name.upper(), {}))
            brawler_rows.append(
                {
                    "name": name,
                    "attempts": values["attempts"],
                    "wins": values["wins"],
                    "win_rate": _percent(values["wins"], values["attempts"]),
                    "image_url": metadata.get("portrait_url") or metadata.get("emoji_url"),
                }
            )
        brawler_rows.sort(key=lambda row: (-row["wins"], -row["attempts"], row["name"]))
        metadata = dict((map_metadata or {}).get(stage["map"], {}))
        stage_cards.append(
            {
                **stage,
                "attempts": len(attempts),
                "wins": wins,
                "losses": losses,
                "draws": draws,
                "win_rate": _percent(wins, len(attempts)),
                "entrants": len(entrants),
                "reached_players": reached_players,
                "cleared_players": cleared_players,
                "stuck_players": stuck_players,
                "top_brawlers": brawler_rows[:3],
                "image_url": metadata.get("image_url"),
            }
        )

    all_today_attempts = [attempt for attempts in stage_attempts.values() for attempt in attempts]
    total_wins = sum(attempt["result"] == "victory" for attempt in all_today_attempts)
    total_losses = sum(attempt["result"] == "defeat" for attempt in all_today_attempts)
    total_draws = sum(attempt["result"] == "draw" for attempt in all_today_attempts)
    completed = sum(player["completed"] for player in players)
    perfect = sum(player["perfect"] for player in players)
    eliminated = sum(player["eliminated"] for player in players)
    active = len(players) - completed - eliminated
    brawler_counter = Counter(attempt["brawler"] for attempt in all_today_attempts)

    return {
        "players": players,
        "stages": stage_cards,
        "summary": {
            "players": len(players),
            "started_today": sum(player["started_today"] for player in players),
            "attempts": len(all_today_attempts),
            "wins": total_wins,
            "losses": total_losses,
            "draws": total_draws,
            "win_rate": _percent(total_wins, len(all_today_attempts)),
            "completed": completed,
            "perfect": perfect,
            "eliminated": eliminated,
            "active": active,
            "completion_rate": _percent(completed, len(players)),
            "most_used_brawler": brawler_counter.most_common(1)[0][0] if brawler_counter else None,
        },
        "latest_observation": latest_observation,
        "event_brawler_limit": EVENT_BRAWLER_LIMIT,
    }
