#!/usr/bin/env python3
"""Phase D entry point. Never fit unless the Phase C evidence gate passes."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import connect
from models.loadout_feature_contract import OFFLINE_DATASET_SQL, STAGE_FEATURES, cumulative_features
from models.loadout_evaluation_contract import PHASE_D_SPLITS, evaluation_gate_passes, phase_d_stages
from scripts.audit_data_pipelines import _json_default, audit

FEATURE_VERSION = "loadout_availability_v1"
ARTIFACT_ROOT = ROOT / "models" / "artifacts" / "loadout"


def canonical_sha256(value: dict) -> str:
    payload = json.dumps(value, default=_json_default, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def dataset_manifest(conn, cutoff: datetime, readiness: dict) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT count(*),count(DISTINCT observed_from_tag),min(battle_time),max(battle_time),
                      count(*) FILTER (WHERE win_label=1),count(*) FILTER (WHERE win_label=0),
                      min(max_available_stage),max(max_available_stage)
               FROM loadout_lobby_features_3v3
               WHERE feature_version=%s AND battle_time<=%s""",
            (FEATURE_VERSION, cutoff),
        )
        rows, players, first_at, last_at, wins, losses, min_stage, max_stage = cur.fetchone()
        cur.execute("SELECT max(id),max(completed_at) FROM loadout_feature_builds WHERE status='complete' AND feature_version=%s", (FEATURE_VERSION,))
        build_id, build_at = cur.fetchone()
    return {
        "feature_version": FEATURE_VERSION,
        "source_cutoff": cutoff,
        "rows": rows,
        "focal_players": players,
        "first_battle_at": first_at,
        "last_battle_at": last_at,
        "labels": {"wins": wins, "losses": losses},
        "available_stage_range": [min_stage, max_stage],
        "feature_build": {"id": build_id, "completed_at": build_at},
        "stages": {str(stage): STAGE_FEATURES[stage] for stage in phase_d_stages()},
        "splits": list(PHASE_D_SPLITS),
        "readiness": readiness,
        "anti_leakage": "battle_time <= cutoff; profile/reference known_at <= battle_time; fold-local transforms only",
    }


def load_dataset(conn, cutoff: datetime) -> pd.DataFrame:
    return pd.read_sql(OFFLINE_DATASET_SQL, conn, params={"cutoff": cutoff})


def estimator(categorical: list[str], numeric: list[str]) -> Pipeline:
    pre = ColumnTransformer([
        ("numeric", Pipeline([
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
        ]), numeric),
        ("categorical", Pipeline([
            ("impute", SimpleImputer(strategy="most_frequent")),
            ("onehot", OneHotEncoder(handle_unknown="ignore", min_frequency=10)),
        ]), categorical),
    ])
    return Pipeline([("pre", pre), ("clf", LogisticRegression(C=.3, max_iter=4000, solver="liblinear"))])


def expected_calibration_error(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0, 1, bins + 1)
    total = len(y)
    return float(sum(
        (mask.sum() / total) * abs(float(y[mask].mean()) - float(p[mask].mean()))
        for index in range(bins)
        if (mask := ((p >= edges[index]) & (p <= edges[index + 1] if index == bins - 1 else p < edges[index + 1]))).any()
    ))


def score_metrics(y: np.ndarray, p: np.ndarray) -> dict:
    base = np.full(len(y), float(y.mean()))
    brier = float(brier_score_loss(y, p))
    base_brier = float(brier_score_loss(y, base))
    rng = np.random.default_rng(20260720)
    auc_samples = []
    for _ in range(200):
        sample = rng.integers(0, len(y), len(y))
        if len(np.unique(y[sample])) == 2:
            auc_samples.append(float(roc_auc_score(y[sample], p[sample])))
    return {
        "roc_auc": float(roc_auc_score(y, p)) if len(np.unique(y)) == 2 else None,
        "brier": brier,
        "log_loss": float(log_loss(y, p, labels=[0, 1])),
        "ece": expected_calibration_error(y, p),
        "base_rate_brier": base_brier,
        "brier_improvement": (base_brier - brier) / base_brier if base_brier else 0.0,
        "confidence_interval": {
            "roc_auc_95": [float(np.quantile(auc_samples, .025)), float(np.quantile(auc_samples, .975))]
            if auc_samples else None,
        },
    }


def player_weights(frame: pd.DataFrame) -> np.ndarray:
    counts = frame.groupby("observed_from_tag")["observed_from_tag"].transform("count")
    weights = 1.0 / counts.to_numpy(dtype=float)
    return weights / weights.mean()


def split_definitions(frame: pd.DataFrame) -> list[tuple[str, int, np.ndarray, np.ndarray]]:
    frame = frame.sort_values("battle_time", kind="stable").reset_index(drop=True)
    n = len(frame)
    definitions = []
    for fold, (train_fraction, test_fraction) in enumerate(((.55, .15), (.70, .15), (.85, .15)), 1):
        train_end = int(n * train_fraction)
        test_end = min(n, train_end + int(n * test_fraction))
        definitions.append(("rolling_chronological", fold, np.arange(train_end), np.arange(train_end, test_end)))
    players = sorted(frame.observed_from_tag.dropna().unique())
    configured = {v.strip().upper() for v in os.getenv("STARRLABS_EVAL_HOLDOUT_TAGS", "").split(",") if v.strip()}
    designated = sorted(set(players) & configured)
    holdout = designated or players[:1]
    unseen = set(players[-max(1, len(players) // 5):])
    balanced = set(players[::max(1, len(players) // 5)][:max(1, len(players) // 5)])
    for name, held in (("designated_player_holdout", set(holdout)),
                       ("balanced_multi_player_leaveout", balanced),
                       ("completely_unseen_players", unseen)):
        test = np.flatnonzero(frame.observed_from_tag.isin(held).to_numpy())
        train = np.flatnonzero((~frame.observed_from_tag.isin(held)).to_numpy())
        definitions.append((name, 0, train, test))
    return definitions


def persist_metric(cur, run_id: int, stage: int, split_name: str, fold: int,
                   test: pd.DataFrame, metric: dict, subgroup: dict, passed: bool) -> None:
    cur.execute(
        """INSERT INTO loadout_model_stage_metrics
           (evaluation_run_id,stage,split_name,fold_index,row_count,player_count,
            roc_auc,brier,log_loss,ece,base_rate_brier,brier_improvement,
            confidence_interval,subgroup_metrics,passed)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)""",
        (run_id, stage, split_name, fold, len(test), test.observed_from_tag.nunique(),
         metric["roc_auc"], metric["brier"], metric["log_loss"], metric["ece"],
         metric["base_rate_brier"], metric["brier_improvement"],
         json.dumps(metric["confidence_interval"]), json.dumps(subgroup), passed),
    )
    for name, unit in (("roc_auc", "score"), ("brier", "score"),
                       ("log_loss", "score"), ("ece", "score"),
                       ("brier_improvement", "ratio")):
        value = metric[name]
        if value is None:
            continue
        threshold, operator = {
            "roc_auc": (.65, "gte"), "ece": (.05, "lte"),
            "brier_improvement": (.05, "gte"),
        }.get(name, (None, None))
        threshold_passed = None
        if threshold is not None:
            threshold_passed = value >= threshold if operator == "gte" else value <= threshold
        cur.execute(
            """INSERT INTO model_metric_history
               (captured_at,model_key,metric_name,metric_value,threshold_value,
                threshold_operator,threshold_passed,unit,phase,source_type,dimensions,metadata)
               VALUES (now(),'loadout_availability_3v3',%s,%s,%s,%s,%s,%s,
                       'phase_d_evaluation','loadout_evaluation',%s::jsonb,%s::jsonb)""",
            (name, value, threshold, operator, threshold_passed, unit,
             json.dumps({"stage": stage, "split": split_name, "fold": fold}),
             json.dumps({"evaluation_run_id": run_id, "rows": len(test)})),
        )


def evaluate(conn, run_id: int, frame: pd.DataFrame, manifest: dict, digest: str) -> dict:
    if frame.empty:
        raise RuntimeError("eligible dataset is empty")
    results = {}
    final_model = None
    for stage in phase_d_stages():
        feature_set = cumulative_features(stage)
        features = feature_set["categorical"] + feature_set["numeric"]
        stage_frame = frame if stage <= 2 else frame[frame.max_available_stage >= stage].copy()
        stage_rows = []
        for split_name, fold, train_idx, test_idx in split_definitions(stage_frame):
            train, test = stage_frame.iloc[train_idx], stage_frame.iloc[test_idx]
            if len(train) < 500 or len(test) < 100 or train.win_label.nunique() < 2 or test.win_label.nunique() < 2:
                raise RuntimeError(f"invalid {split_name} fold {fold}: train={len(train)} test={len(test)}")
            model = estimator(feature_set["categorical"], feature_set["numeric"])
            model.fit(train[features], train.win_label.astype(int), clf__sample_weight=player_weights(train))
            probability = model.predict_proba(test[features])[:, 1]
            metric = score_metrics(test.win_label.astype(int).to_numpy(), probability)
            subgroup = {}
            for mode, positions in test.reset_index(drop=True).groupby("mode").groups.items():
                positions = np.asarray(list(positions), dtype=int)
                labels = test.reset_index(drop=True).win_label.astype(int).to_numpy()[positions]
                if len(positions) >= 30 and len(np.unique(labels)) == 2:
                    subgroup[mode] = score_metrics(labels, probability[positions])
            major_collapse = any(
                values["roc_auc"] is not None and values["roc_auc"] < .55
                for values in subgroup.values()
            )
            passed = (metric["roc_auc"] is not None and metric["roc_auc"] >= .65
                      and metric["ece"] <= .05 and not major_collapse)
            with conn.cursor() as cur:
                persist_metric(cur, run_id, stage, split_name, fold, test, metric, subgroup, passed)
            stage_rows.append({"split": split_name, "fold": fold,
                               "subgroup_metrics": subgroup,
                               "major_subgroup_collapse": major_collapse, **metric})
        results[str(stage)] = stage_rows
        if stage == 4:
            final_model = estimator(feature_set["categorical"], feature_set["numeric"])
            final_model.fit(stage_frame[features], stage_frame.win_label.astype(int),
                            clf__sample_weight=player_weights(stage_frame))

    unseen = [row for row in results["4"] if row["split"] == "completely_unseen_players"][0]
    gate_metrics = {
        "unseen_player_auc": unseen["roc_auc"] or 0,
        "brier_improvement_over_base": unseen["brier_improvement"],
        "ece": unseen["ece"],
        "major_subgroup_collapse": unseen["major_subgroup_collapse"],
    }
    eligible = evaluation_gate_passes(gate_metrics)
    artifact_dir = ARTIFACT_ROOT / str(run_id)
    artifact_dir.mkdir(parents=True, exist_ok=False)
    joblib.dump(final_model, artifact_dir / "stage4.joblib")
    report = {"manifest": manifest, "manifest_sha256": digest, "results": results,
              "gate_metrics": gate_metrics, "promotion_eligible": eligible}
    (artifact_dir / "evaluation.json").write_text(json.dumps(report, default=_json_default, indent=2, sort_keys=True))
    return {"artifact_path": str(artifact_dir), "promotion_eligible": eligible, **gate_metrics}


def main() -> int:
    readiness_result = audit(persist=False)
    readiness = readiness_result["metrics"]["item_model"]
    cutoff = datetime.now(timezone.utc)
    with connect() as conn:
        manifest = dataset_manifest(conn, cutoff, readiness)
        digest = canonical_sha256(manifest)
        if not readiness_result["item_model_ready"]:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO loadout_model_evaluation_runs
                       (status,completed_at,feature_version,source_cutoff,readiness_snapshot,
                        dataset_manifest,dataset_manifest_sha256,blockers)
                       VALUES ('blocked_by_phase_c',now(),%s,%s,%s::jsonb,%s::jsonb,%s,%s::jsonb)
                       RETURNING id""",
                    (FEATURE_VERSION, cutoff, json.dumps(readiness, default=_json_default),
                     json.dumps(manifest, default=_json_default), digest,
                     json.dumps(readiness["blockers"])),
                )
                run_id = cur.fetchone()[0]
            print("LOADOUT_PHASE_D_BLOCKED|" + json.dumps({
                "evaluation_run_id": run_id, "manifest_sha256": digest,
                "blockers": readiness["blockers"], "trained": False,
            }, sort_keys=True))
            return 2

        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO loadout_model_evaluation_runs
                   (status,feature_version,source_cutoff,readiness_snapshot,dataset_manifest,dataset_manifest_sha256)
                   VALUES ('running',%s,%s,%s::jsonb,%s::jsonb,%s) RETURNING id""",
                (FEATURE_VERSION, cutoff, json.dumps(readiness, default=_json_default),
                 json.dumps(manifest, default=_json_default), digest),
            )
            run_id = cur.fetchone()[0]
        try:
            frame = load_dataset(conn, cutoff)
            result = evaluate(conn, run_id, frame, manifest, digest)
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE loadout_model_evaluation_runs
                       SET status='complete',completed_at=now(),artifact_path=%s,promotion_eligible=%s
                       WHERE id=%s""",
                    (result["artifact_path"], result["promotion_eligible"], run_id),
                )
            print("LOADOUT_PHASE_D_COMPLETE|" + json.dumps({"evaluation_run_id": run_id, **result}, sort_keys=True))
            return 0 if result["promotion_eligible"] else 3
        except Exception as exc:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE loadout_model_evaluation_runs SET status='failed',completed_at=now(),error_message=%s WHERE id=%s",
                    (str(exc)[:2000], run_id),
                )
            raise


if __name__ == "__main__":
    raise SystemExit(main())
