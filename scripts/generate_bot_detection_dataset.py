#!/usr/bin/env python3
"""Generate an auditable post-battle bot-detection dataset from trusted labels."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from database import connect
from services.bot_detection import participant_evidence


RULES_VERSION = "post_battle_bot_rules_v1"


SQL = """
SELECT l.id AS label_id, l.observed_from_tag, l.battle_time, l.label AS lobby_label,
       l.source AS label_source, l.confidence AS label_confidence, l.notes AS label_notes,
       b.player_tag, b.player_name, b.brawler_id, b.brawler_name, b.brawler_power,
       b.brawler_trophies, b.player_total_trophies, b.player_level, b.rank,
       p.captured_at AS current_profile_captured_at,
       p.trophies AS current_profile_trophies,
       p.highest_trophies AS current_profile_highest_trophies,
       p.exp_level AS current_profile_exp_level,
       p.victories_3v3 AS current_victories_3v3,
       p.solo_victories AS current_solo_victories,
       p.duo_victories AS current_duo_victories,
       COALESCE(pb.owned_brawlers, 0) AS current_owned_brawlers
FROM bot_lobby_labels l
JOIN battles b ON b.battletime = l.battle_time
              AND b.observed_from_tag = l.observed_from_tag
LEFT JOIN LATERAL (
  SELECT ps.* FROM player_profile_snapshots ps
  WHERE ps.player_tag = b.player_tag
  ORDER BY (ps.captured_at >= COALESCE(b.battletime_ts, b.battletime::timestamptz)) DESC,
           CASE WHEN ps.captured_at >= COALESCE(b.battletime_ts, b.battletime::timestamptz)
                THEN ps.captured_at END ASC,
           ps.captured_at DESC
  LIMIT 1
) p ON true
LEFT JOIN LATERAL (
  SELECT count(*)::int AS owned_brawlers
  FROM player_brawler_snapshots x WHERE x.profile_snapshot_id = p.id
) pb ON true
WHERE b.player_tag <> l.observed_from_tag
ORDER BY l.battle_time, b.rank NULLS LAST, b.player_tag
"""


def generate(output_dir: Path) -> dict:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(SQL)
        columns = [column.name for column in cur.description]
        source_rows = [dict(zip(columns, row)) for row in cur.fetchall()]

    rows = []
    for source in source_rows:
        scout = "cached" if source.get("current_profile_captured_at") else "unavailable"
        evidence = participant_evidence(source, scout)
        actual_control = (
            "bot" if source["lobby_label"] == "bots"
            else "human" if source["lobby_label"] == "humans"
            else "unknown"
        )
        rows.append({
            "label_id": source["label_id"],
            "observed_from_tag": source["observed_from_tag"],
            "battle_time": source["battle_time"],
            "lobby_label": source["lobby_label"],
            "participant_actual_control": actual_control,
            "label_source": source["label_source"],
            "label_confidence": source["label_confidence"],
            "label_notes": source["label_notes"],
            "player_tag": source["player_tag"],
            "player_name": source["player_name"],
            "brawler_id": source["brawler_id"],
            "brawler_name": source["brawler_name"],
            "brawler_power": source["brawler_power"],
            "battle_brawler_trophies": source["brawler_trophies"],
            "rule_prediction": evidence["control_kind"],
            "hard_bot_signal": evidence["hard_bot_signal"],
            "account_status": evidence["account_kind"],
            "evidence_reasons": evidence["evidence_reasons"],
            "features": evidence["features"],
            "rules_version": RULES_VERSION,
        })

    generated = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / f"bot-detection-{generated}.jsonl"
    csv_path = output_dir / f"bot-detection-{generated}.csv"
    jsonl_bytes = "".join(json.dumps(row, sort_keys=True, default=str) + "\n" for row in rows).encode()
    jsonl_path.write_bytes(jsonl_bytes)

    flat_rows = []
    for row in rows:
        flat = {key: value for key, value in row.items() if key not in {"features", "evidence_reasons"}}
        flat["evidence_reasons"] = "|".join(row["evidence_reasons"])
        flat.update(row["features"])
        flat_rows.append(flat)
    if flat_rows:
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(flat_rows[0]))
            writer.writeheader()
            writer.writerows(flat_rows)
    else:
        csv_path.write_text("", encoding="utf-8")

    digest = hashlib.sha256(jsonl_bytes).hexdigest()
    manifest = {
        "generated_at": generated,
        "rules_version": RULES_VERSION,
        "lobby_count": len({row["label_id"] for row in rows}),
        "row_count": len(rows),
        "trusted_lobbies": sum(row["label_confidence"] == "trusted" for row in rows) // 9,
        "sha256_jsonl": digest,
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "label_distribution": {label: len({r["label_id"] for r in rows if r["lobby_label"] == label}) for label in ("bots", "humans", "mixed", "unsure")},
        "warning": "Rule predictions are features/baselines, never substituted for trusted labels.",
    }
    manifest_path = output_dir / f"bot-detection-{generated}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO bot_detection_dataset_exports
               (row_count, lobby_count, sha256, csv_path, jsonl_path, rules_version, metadata)
               VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)""",
            (manifest["row_count"], manifest["lobby_count"], digest, str(csv_path), str(jsonl_path), RULES_VERSION, json.dumps(manifest)),
        )
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("datasets/bot_detection"))
    args = parser.parse_args()
    print(json.dumps(generate(args.output_dir), indent=2, sort_keys=True))
