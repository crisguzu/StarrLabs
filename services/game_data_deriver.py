"""Build point-in-time brawler mechanics and map geometry from BrawlAPI snapshots."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, deque
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from dotenv import load_dotenv
from psycopg2.extras import execute_values

from database import connect


load_dotenv(Path(__file__).resolve().parents[1] / ".env")
PROVIDER = "brawlapi"

REQUIRED_PATHS = (
    "v1/brawlers",
    "v1/maps",
    "v2/raw/csv_logic/locations",
    "v2/raw/csv_logic/maps",
    "v2/raw/csv_logic/tiles",
    "v2/raw/csv_logic/characters",
    "v2/raw/csv_logic/skills",
    "v2/raw/csv_logic/class_archetypes",
    "v2/raw/csv_logic/traits",
)

BRAWLER_DIFF_FIELDS = (
    "speed", "base_hitpoints", "attack_range", "attack_damage", "attack_bullets",
    "reload_ms", "super_range", "super_damage", "super_bullets",
    "can_walk_over_water", "offense_rating", "defense_rating", "utility_rating",
)
MAP_DIFF_FIELDS = (
    "width", "height", "open_ratio", "movement_blocked_ratio",
    "projectile_blocked_ratio", "destructible_ratio", "bush_ratio", "water_ratio",
    "choke_ratio", "longest_sightline", "box_marker_count", "spawn_marker_count",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _ratio(value: int, total: int) -> float:
    return round(value / total, 6) if total else 0.0


def _raw_data(payload: Dict[str, Any]) -> Any:
    return payload.get("data", {}) if isinstance(payload, dict) else {}


def tile_codebook(tile_rows: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Choose one mechanics definition per encoded tile code."""
    result: Dict[str, Dict[str, Any]] = {}
    for key, row in tile_rows.items():
        code = row.get("TileCode")
        if not code:
            continue
        current = result.get(code)
        # Several metadata-only marker rows reuse '.', while Open is the real
        # grid mechanic. Prefer it deterministically.
        if current is None or row.get("Name") == "Open":
            result[code] = row
    return result


def _longest_clear_line(blocked: Sequence[Sequence[bool]]) -> int:
    if not blocked:
        return 0
    height, width = len(blocked), max(len(row) for row in blocked)
    best = 0
    for row in blocked:
        run = 0
        for cell in row:
            run = 0 if cell else run + 1
            best = max(best, run)
    for x in range(width):
        run = 0
        for y in range(height):
            cell = blocked[y][x] if x < len(blocked[y]) else True
            run = 0 if cell else run + 1
            best = max(best, run)
    return best


def _largest_component_and_chokes(walkable: Sequence[Sequence[bool]]) -> Tuple[int, int]:
    if not walkable:
        return 0, 0
    height = len(walkable)
    width = max(len(row) for row in walkable)
    seen = set()
    largest = 0
    chokes = 0
    for y in range(height):
        for x in range(len(walkable[y])):
            if not walkable[y][x]:
                continue
            neighbors = [
                (ny, nx) for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1))
                if 0 <= ny < height and 0 <= nx < len(walkable[ny]) and walkable[ny][nx]
            ]
            if 0 < y < height - 1 and 0 < x < width - 1 and len(neighbors) <= 2:
                chokes += 1
            if (y, x) in seen:
                continue
            queue = deque([(y, x)])
            seen.add((y, x))
            size = 0
            while queue:
                cy, cx = queue.popleft()
                size += 1
                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if (
                        0 <= ny < height and 0 <= nx < len(walkable[ny])
                        and walkable[ny][nx] and (ny, nx) not in seen
                    ):
                        seen.add((ny, nx))
                        queue.append((ny, nx))
            largest = max(largest, size)
    return largest, chokes


def _symmetry(grid: Sequence[str]) -> Tuple[float, float]:
    if not grid:
        return 0.0, 0.0
    horizontal_total = horizontal_equal = 0
    vertical_total = vertical_equal = 0
    for row in grid:
        for x, value in enumerate(row):
            mirror = len(row) - 1 - x
            if x <= mirror:
                horizontal_total += 1
                horizontal_equal += value == row[mirror]
    for y, row in enumerate(grid):
        mirror_y = len(grid) - 1 - y
        if y > mirror_y:
            continue
        other = grid[mirror_y]
        for x in range(min(len(row), len(other))):
            vertical_total += 1
            vertical_equal += row[x] == other[x]
    return _ratio(horizontal_equal, horizontal_total), _ratio(vertical_equal, vertical_total)


def derive_map_geometry(grid: Sequence[str], codebook: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    height = len(grid)
    width = max((len(row) for row in grid), default=0)
    total = sum(len(row) for row in grid)
    counts = Counter(char for row in grid for char in row)
    categories = Counter()
    movement_grid: List[List[bool]] = []
    projectile_grid: List[List[bool]] = []
    unknown = 0
    for row in grid:
        movement_row = []
        projectile_row = []
        for code in row:
            tile = codebook.get(code)
            marker = code.isdigit()
            if tile is None and not marker:
                unknown += 1
            tile = tile or {}
            move_blocked = bool(tile.get("BlocksMovement", False))
            projectile_blocked = bool(tile.get("BlocksProjectiles", False))
            movement_row.append(not move_blocked)
            projectile_row.append(projectile_blocked)
            categories["movement_blocked"] += move_blocked
            categories["projectile_blocked"] += projectile_blocked
            categories["destructible"] += bool(tile.get("IsDestructible", False))
            categories["bush"] += bool(tile.get("IsForest", False))
            categories["water"] += str(tile.get("Name", "")).lower() in ("water", "invisiblewater")
            categories["damaging"] += bool(tile.get("Damage", 0)) or str(tile.get("Name", "")).lower() in ("damage", "intervaldamage")
            categories["speed_effect"] += bool(tile.get("SpeedChange", 0)) or str(tile.get("Name", "")).lower() in ("slow", "fast", "ice", "snow")
        movement_grid.append(movement_row)
        projectile_grid.append(projectile_row)
    largest, chokes = _largest_component_and_chokes(movement_grid)
    hsym, vsym = _symmetry(grid)
    walkable = total - categories["movement_blocked"]
    known = total - unknown
    return {
        "width": width,
        "height": height,
        "tile_count": total,
        "open_ratio": _ratio(total - categories["movement_blocked"] - categories["bush"], total),
        "movement_blocked_ratio": _ratio(categories["movement_blocked"], total),
        "projectile_blocked_ratio": _ratio(categories["projectile_blocked"], total),
        "destructible_ratio": _ratio(categories["destructible"], total),
        "bush_ratio": _ratio(categories["bush"], total),
        "water_ratio": _ratio(categories["water"], total),
        "damaging_ratio": _ratio(categories["damaging"], total),
        "speed_effect_ratio": _ratio(categories["speed_effect"], total),
        "choke_ratio": _ratio(chokes, walkable),
        "largest_walkable_component_ratio": _ratio(largest, walkable),
        "horizontal_symmetry": hsym,
        "vertical_symmetry": vsym,
        "longest_sightline": _longest_clear_line(projectile_grid),
        "box_marker_count": counts.get("0", 0),
        "spawn_marker_count": sum(counts.get(str(i), 0) for i in range(1, 10)),
        "unknown_tile_ratio": _ratio(unknown, total),
        "tile_counts": dict(counts),
        "coverage_confidence": round(known / total, 4) if total else 0.0,
    }


def derive_brawler(
    character: Dict[str, Any], display: Optional[Dict[str, Any]],
    skills: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    attack = skills.get(character.get("WeaponSkill"), {}) or {}
    super_skill = skills.get(character.get("UltimateSkill"), {}) or {}
    traits = character.get("Traits")
    if traits is None:
        traits_list: List[Any] = []
    elif isinstance(traits, list):
        traits_list = traits
    else:
        traits_list = [traits]
    values = {
        "speed": character.get("Speed"),
        "base_hitpoints": character.get("Hitpoints"),
        "attack_range": attack.get("CastingRange"),
        "attack_damage": attack.get("Damage"),
        "attack_bullets": attack.get("NumBulletsInOneAttack"),
        "reload_ms": attack.get("RechargeTime"),
        "super_range": super_skill.get("CastingRange"),
        "super_damage": super_skill.get("Damage"),
        "super_bullets": super_skill.get("NumBulletsInOneAttack"),
        "can_walk_over_water": bool(character.get("CanWalkOverWater", False)),
        "offense_rating": character.get("OffenseRating"),
        "defense_rating": character.get("DefenseRating"),
        "utility_rating": character.get("UtilityRating"),
    }
    required = ("speed", "base_hitpoints", "attack_range", "attack_damage", "reload_ms")
    missing = [field for field in required if values.get(field) is None]
    return {
        "brawler_id": int(character["id"]),
        "internal_name": character.get("Name"),
        "display_name": (display or {}).get("name") or character.get("ItemName"),
        "class_archetype": character.get("ClassArchetype"),
        "movement_type": character.get("MovementType"),
        **values,
        "traits": traits_list,
        "mechanics": {
            "weapon_skill": character.get("WeaponSkill"),
            "super_skill": character.get("UltimateSkill"),
            "attack_projectiles": attack.get("Projectiles"),
            "super_projectiles": super_skill.get("Projectiles"),
            "attack_spread": attack.get("Spread"),
            "super_spread": super_skill.get("Spread"),
            "overcharged_super": character.get("OverchargedUltimateSkill"),
            "unique_property": character.get("UniqueProperty"),
        },
        "coverage_confidence": round((len(required) - len(missing)) / len(required), 4),
        "missing_fields": missing,
    }


def latest_snapshots(conn) -> Dict[str, Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (dataset_path)
                   id, dataset_path, payload_hash, schema_fingerprint, payload,
                   upstream_generated_at, first_captured_at
            FROM third_party_reference_snapshots
            WHERE provider = %s
            ORDER BY dataset_path, last_observed_at DESC, id DESC
            """,
            (PROVIDER,),
        )
        return {
            row[1]: {
                "id": row[0], "path": row[1], "hash": row[2], "schema": row[3],
                "payload": row[4], "generated_at": row[5], "captured_at": row[6],
            }
            for row in cur.fetchall()
        }


def materialize_game_data_versions(conn, snapshots: Dict[str, Dict[str, Any]]) -> None:
    with conn.cursor() as cur:
        for path, snapshot in snapshots.items():
            effective = snapshot["generated_at"] or snapshot["captured_at"]
            cur.execute(
                """
                SELECT id, effective_from FROM game_data_versions
                WHERE provider=%s AND dataset_path=%s
                ORDER BY effective_from DESC, id DESC LIMIT 1
                """,
                (PROVIDER, path),
            )
            previous = cur.fetchone()
            cur.execute(
                "SELECT 1 FROM game_data_versions WHERE provider=%s AND dataset_path=%s AND snapshot_id=%s",
                (PROVIDER, path, snapshot["id"]),
            )
            if cur.fetchone():
                continue
            if previous and effective <= previous[1]:
                effective = max(snapshot["captured_at"], previous[1] + timedelta(microseconds=1))
            if previous:
                cur.execute("UPDATE game_data_versions SET effective_to=%s WHERE id=%s AND effective_to IS NULL", (effective, previous[0]))
            cur.execute(
                """
                INSERT INTO game_data_versions (
                  provider,dataset_path,snapshot_id,payload_hash,schema_fingerprint,
                  effective_from,known_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    PROVIDER, path, snapshot["id"], snapshot["hash"], snapshot["schema"],
                    effective, snapshot["captured_at"],
                ),
            )


def _insert_balance_changes(conn, run_id: int, previous_run_id: Optional[int], effective_at, known_at) -> int:
    if not previous_run_id:
        return 0
    changes: List[Tuple[Any, ...]] = []
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM brawler_mechanics_versions WHERE derivation_run_id=%s", (previous_run_id,))
        cols = [d.name for d in cur.description]
        old_brawlers = {row[cols.index("brawler_id")]: dict(zip(cols, row)) for row in cur.fetchall()}
        cur.execute("SELECT * FROM brawler_mechanics_versions WHERE derivation_run_id=%s", (run_id,))
        cols = [d.name for d in cur.description]
        for row in cur.fetchall():
            new = dict(zip(cols, row)); old = old_brawlers.get(new["brawler_id"])
            if not old: continue
            for field in BRAWLER_DIFF_FIELDS:
                if old.get(field) != new.get(field):
                    changes.append((run_id,effective_at,known_at,"brawler",new["brawler_id"],field,json.dumps(old.get(field)),json.dumps(new.get(field))))
        cur.execute("SELECT * FROM map_geometry_versions WHERE derivation_run_id=%s", (previous_run_id,))
        cols = [d.name for d in cur.description]
        old_maps = {row[cols.index("location_id")]: dict(zip(cols, row)) for row in cur.fetchall()}
        cur.execute("SELECT * FROM map_geometry_versions WHERE derivation_run_id=%s", (run_id,))
        cols = [d.name for d in cur.description]
        for row in cur.fetchall():
            new = dict(zip(cols, row)); old = old_maps.get(new["location_id"])
            if not old: continue
            for field in MAP_DIFF_FIELDS:
                if old.get(field) != new.get(field):
                    changes.append((run_id,effective_at,known_at,"map",new["location_id"],field,json.dumps(old.get(field),default=str),json.dumps(new.get(field),default=str)))
        if changes:
            execute_values(cur,"""INSERT INTO reference_balance_changes
              (derivation_run_id,effective_at,known_at,entity_type,entity_id,field_name,old_value,new_value)
              VALUES %s ON CONFLICT DO NOTHING""",changes,template="(%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)")
    return len(changes)


def derive_current() -> Dict[str, Any]:
    conn = connect()
    try:
        snapshots = latest_snapshots(conn)
        missing = sorted(set(REQUIRED_PATHS) - set(snapshots))
        if missing:
            raise RuntimeError(f"Missing required BrawlAPI snapshots: {missing}")
        materialize_game_data_versions(conn, snapshots)
        bundle = {path: snapshots[path]["hash"] for path in sorted(snapshots)}
        bundle_hash = hashlib.sha256(_canonical(bundle).encode("utf-8")).hexdigest()
        effective_from = max((s["generated_at"] or s["captured_at"]) for s in snapshots.values())
        known_at = max(s["captured_at"] for s in snapshots.values())
        with conn.cursor() as cur:
            cur.execute("SELECT id,status,metrics FROM reference_derivation_runs WHERE input_bundle_hash=%s", (bundle_hash,))
            existing = cur.fetchone()
            if existing and existing[1] == "complete":
                conn.commit()
                return {"run_id": existing[0], "unchanged": True, **(existing[2] or {})}
            cur.execute("SELECT id,effective_from FROM reference_derivation_runs WHERE status='complete' ORDER BY effective_from DESC,id DESC LIMIT 1")
            previous = cur.fetchone()
            if previous and effective_from <= previous[1]:
                effective_from = max(known_at, previous[1] + timedelta(microseconds=1))
            if previous:
                cur.execute("UPDATE reference_derivation_runs SET effective_to=%s WHERE id=%s AND effective_to IS NULL", (effective_from, previous[0]))
            cur.execute(
                """INSERT INTO reference_derivation_runs
                   (provider,input_bundle_hash,effective_from,known_at,status,input_snapshot_ids)
                   VALUES (%s,%s,%s,%s,'running',%s::jsonb) RETURNING id""",
                (PROVIDER,bundle_hash,effective_from,known_at,json.dumps({p:s["id"] for p,s in snapshots.items()})),
            )
            run_id = cur.fetchone()[0]

        curated_brawlers = {int(x["id"]): x for x in snapshots["v1/brawlers"]["payload"].get("list", [])}
        characters = _raw_data(snapshots["v2/raw/csv_logic/characters"]["payload"])
        skills = _raw_data(snapshots["v2/raw/csv_logic/skills"]["payload"])
        brawlers = [derive_brawler(row, curated_brawlers.get(int(row["id"])), skills) for row in characters.values() if row.get("id") in curated_brawlers]

        curated_maps = {int(x["id"]): x for x in snapshots["v1/maps"]["payload"].get("list", [])}
        locations = _raw_data(snapshots["v2/raw/csv_logic/locations"]["payload"])
        maps = _raw_data(snapshots["v2/raw/csv_logic/maps"]["payload"])
        tiles = tile_codebook(_raw_data(snapshots["v2/raw/csv_logic/tiles"]["payload"]))
        map_rows = []
        missing_map_grids = 0
        for internal_location_name, location in locations.items():
            internal_map_name = location.get("Map")
            map_record = maps.get(internal_map_name)
            if not internal_map_name or not map_record or not map_record.get("Data"):
                missing_map_grids += 1
                continue
            geometry = derive_map_geometry(map_record["Data"], tiles)
            public = curated_maps.get(int(location["id"]), {})
            map_rows.append({
                "location_id": int(location["id"]),
                "internal_location_name": internal_location_name,
                "display_name": public.get("name"),
                "game_mode_variation": location.get("GameModeVariation"),
                "internal_map_name": internal_map_name,
                **geometry,
                "geometry": {"map_record_id": map_record.get("id"), "disabled": location.get("Disabled")},
            })

        with conn.cursor() as cur:
            execute_values(cur,"""INSERT INTO brawler_mechanics_versions (
              derivation_run_id,brawler_id,internal_name,display_name,class_archetype,movement_type,
              speed,base_hitpoints,attack_range,attack_damage,attack_bullets,reload_ms,
              super_range,super_damage,super_bullets,can_walk_over_water,offense_rating,
              defense_rating,utility_rating,traits,mechanics,coverage_confidence,missing_fields)
              VALUES %s""",[(run_id,b["brawler_id"],b["internal_name"],b["display_name"],b["class_archetype"],b["movement_type"],b["speed"],b["base_hitpoints"],b["attack_range"],b["attack_damage"],b["attack_bullets"],b["reload_ms"],b["super_range"],b["super_damage"],b["super_bullets"],b["can_walk_over_water"],b["offense_rating"],b["defense_rating"],b["utility_rating"],json.dumps(b["traits"]),json.dumps(b["mechanics"]),b["coverage_confidence"],json.dumps(b["missing_fields"])) for b in brawlers],template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s::jsonb)")
            execute_values(cur,"""INSERT INTO map_geometry_versions (
              derivation_run_id,location_id,internal_location_name,display_name,game_mode_variation,
              internal_map_name,width,height,tile_count,open_ratio,movement_blocked_ratio,
              projectile_blocked_ratio,destructible_ratio,bush_ratio,water_ratio,damaging_ratio,
              speed_effect_ratio,choke_ratio,largest_walkable_component_ratio,horizontal_symmetry,
              vertical_symmetry,longest_sightline,box_marker_count,spawn_marker_count,unknown_tile_ratio,
              tile_counts,geometry,coverage_confidence) VALUES %s""",[(run_id,m["location_id"],m["internal_location_name"],m["display_name"],m["game_mode_variation"],m["internal_map_name"],m["width"],m["height"],m["tile_count"],m["open_ratio"],m["movement_blocked_ratio"],m["projectile_blocked_ratio"],m["destructible_ratio"],m["bush_ratio"],m["water_ratio"],m["damaging_ratio"],m["speed_effect_ratio"],m["choke_ratio"],m["largest_walkable_component_ratio"],m["horizontal_symmetry"],m["vertical_symmetry"],m["longest_sightline"],m["box_marker_count"],m["spawn_marker_count"],m["unknown_tile_ratio"],json.dumps(m["tile_counts"]),json.dumps(m["geometry"]),m["coverage_confidence"]) for m in map_rows],template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)")
        changes = _insert_balance_changes(conn, run_id, previous[0] if previous else None, effective_from, known_at)
        metrics = {
            "brawlers": len(brawlers), "maps": len(map_rows),
            "missing_map_grids": missing_map_grids, "balance_changes": changes,
            "brawler_mean_coverage": round(sum(b["coverage_confidence"] for b in brawlers)/len(brawlers),4) if brawlers else 0,
            "map_mean_coverage": round(sum(m["coverage_confidence"] for m in map_rows)/len(map_rows),4) if map_rows else 0,
        }
        with conn.cursor() as cur:
            cur.execute("UPDATE reference_derivation_runs SET status='complete',completed_at=now(),metrics=%s::jsonb WHERE id=%s",(json.dumps(metrics),run_id))
        conn.commit()
        return {"run_id": run_id, "unchanged": False, **metrics}
    except Exception as exc:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    result = derive_current()
    print("GAME_DATA_DERIVATION_OK|" + "|".join(f"{key}={value}" for key,value in result.items()))


if __name__ == "__main__":
    main()
