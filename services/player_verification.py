"""In-game ownership challenges backed by the official Brawl Stars API."""

import datetime as dt
import os
import random
from urllib.parse import quote

import requests


BASE_URL = "https://api.brawlstars.com/v1"
CHALLENGE_MIN_EVENT_SECONDS = 30 * 60
ALLOWED_MODES = {
    "brawlBall", "gemGrab", "heist", "hotZone", "knockout",
    "bounty", "soloShowdown", "duoShowdown", "wipeout",
}
MODE_SC_IDS = {
    "gemGrab": 48000000,
    "heist": 48000002,
    "bounty": 48000003,
    "brawlBall": 48000005,
    "soloShowdown": 48000006,
    "duoShowdown": 48000009,
    "hotZone": 48000017,
    "knockout": 48000020,
    "wipeout": 48000025,
}


def _headers():
    token = os.getenv("BRAWLSTARS_API_TOKEN") or os.getenv("API_TOKEN")
    if not token:
        raise RuntimeError("BRAWLSTARS_API_TOKEN is not configured")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _get(path):
    response = requests.get(f"{BASE_URL}{path}", headers=_headers(), timeout=(5, 15))
    response.raise_for_status()
    return response.json()


def api_player(tag):
    return _get(f"/players/{quote(tag, safe='')}")


def api_rotation():
    return _get("/events/rotation")


def api_battlelog(tag):
    return _get(f"/players/{quote(tag, safe='')}/battlelog")


def parse_api_time(value):
    for pattern in ("%Y%m%dT%H%M%S.%fZ", "%Y%m%dT%H%M%SZ"):
        try:
            return dt.datetime.strptime(value, pattern).replace(tzinfo=dt.timezone.utc)
        except (TypeError, ValueError):
            continue
    raise ValueError("Invalid Brawl Stars timestamp")


def build_tasks(player, rotation, now=None, rng=None):
    """Choose two unlocked brawlers and two currently playable stable modes."""
    now = now or dt.datetime.now(dt.timezone.utc)
    rng = rng or random.SystemRandom()
    brawlers = [b for b in player.get("brawlers", []) if b.get("id") and b.get("name")]
    if len(brawlers) < 2:
        raise ValueError("This profile needs at least two unlocked brawlers to verify.")

    modes = {}
    for slot in rotation or []:
        event = slot.get("event") or {}
        mode = event.get("mode")
        end = slot.get("endTime")
        if mode not in ALLOWED_MODES or not end:
            continue
        try:
            remaining = (parse_api_time(end) - now).total_seconds()
        except (TypeError, ValueError):
            continue
        if remaining >= CHALLENGE_MIN_EVENT_SECONDS:
            modes.setdefault(mode, {"mode": mode, "map": event.get("map"), "ends_at": end})
    if len(modes) < 2:
        raise ValueError("There are not enough stable live events right now. Try again after the next rotation.")

    selected_brawlers = rng.sample(brawlers, 2)
    selected_modes = rng.sample(list(modes.values()), 2)
    return [
        {
            "step": index + 1,
            "brawler_id": brawler["id"],
            "brawler_name": brawler["name"],
            "brawler_image_url": f"https://cdn.brawlify.com/brawlers/portraits/{brawler['id']}.png",
            "mode_image_url": f"https://cdn.brawlify.com/game-modes/regular/{MODE_SC_IDS[mode['mode']]}.png",
            **mode,
        }
        for index, (brawler, mode) in enumerate(zip(selected_brawlers, selected_modes))
    ]


def player_brawler(battle_item, player_tag):
    target = player_tag.upper()
    battle = battle_item.get("battle") or {}
    players = list(battle.get("players") or [])
    for team in battle.get("teams") or []:
        players.extend(team or [])
    for player in players:
        if str(player.get("tag") or "").upper() == target:
            return player.get("brawler") or {}
    return {}


def matching_progress(items, player_tag, tasks, created_at):
    """Return ordered completed steps using battles that began after challenge creation."""
    candidates = []
    seen_battles = set()
    for item in items or []:
        try:
            played_at = parse_api_time(item.get("battleTime"))
        except (TypeError, ValueError):
            continue
        if played_at < created_at - dt.timedelta(seconds=10):
            continue
        battle_key = (item.get("battleTime"), (item.get("event") or {}).get("id"))
        if battle_key in seen_battles:
            continue
        seen_battles.add(battle_key)
        candidates.append((played_at, item))
    candidates.sort(key=lambda pair: pair[0])

    completed = 0
    matches = []
    for played_at, item in candidates:
        if completed >= len(tasks):
            break
        task = tasks[completed]
        event = item.get("event") or {}
        brawler = player_brawler(item, player_tag)
        if event.get("mode") == task["mode"] and brawler.get("id") == task["brawler_id"]:
            matches.append({"battle_time": item.get("battleTime"), "step": completed + 1})
            completed += 1
    return completed, matches
