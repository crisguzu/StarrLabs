#!/usr/bin/env python3
"""Score active Loadout challengers into the isolated Phase E shadow table."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import joblib
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import connect
from models.loadout_feature_contract import OFFLINE_DATASET_SQL, cumulative_features


def score() -> dict:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id,artifact_path,artifact_sha256 FROM loadout_model_registry WHERE status='shadow' ORDER BY registered_at")
        registry = cur.fetchall()
        total = 0
        for registry_id, artifact_path, expected_sha in registry:
            artifact = Path(artifact_path)
            if hashlib.sha256(artifact.read_bytes()).hexdigest() != expected_sha:
                raise RuntimeError(f"artifact checksum mismatch for registry {registry_id}")
            cur.execute("SELECT max(battle_time) FROM loadout_shadow_scores WHERE model_registry_id=%s", (registry_id,))
            after = cur.fetchone()[0]
            frame = pd.read_sql(OFFLINE_DATASET_SQL, conn, params={"cutoff": pd.Timestamp.utcnow()})
            if after is not None:
                frame = frame[frame.battle_time > after]
            if frame.empty:
                continue
            model = joblib.load(artifact)
            contract = cumulative_features(4)
            features = contract["categorical"] + contract["numeric"]
            probabilities = model.predict_proba(frame[features])[:, 1]
            manifest_sha = expected_sha
            rows = [(registry_id, row.battle_time, row.observed_from_tag, float(probability),
                     int(row.win_label) if pd.notna(row.win_label) else None, 4, manifest_sha)
                    for row, probability in zip(frame.itertuples(), probabilities)]
            cur.executemany(
                """INSERT INTO loadout_shadow_scores
                   (model_registry_id,battle_time,observed_from_tag,probability,outcome,feature_stage,feature_manifest_sha256)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING""", rows)
            total += cur.rowcount
        return {"active_models": len(registry), "scores_written": total, "production_predictions_written": 0}


if __name__ == "__main__":
    print("LOADOUT_SHADOW|" + str(score()))
