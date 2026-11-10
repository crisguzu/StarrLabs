#!/usr/bin/env python3
import os
import sys
import time
import json
import datetime as dt
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np
import psycopg2
from database import connect as database_connect
from psycopg2.extras import execute_batch
import pandas as pd
from dotenv import load_dotenv
import joblib
from catboost import CatBoostClassifier, Pool

import warnings

warnings.filterwarnings(
    "ignore",
    message=r".*encountered in matmul.*",
    category=RuntimeWarning,
    module=r"sklearn\.utils\.extmath",
)

from models.ml_utils import sanitize_matrix

# Load .env
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")

PREDICT_DAYS_BACK = int(os.getenv("PREDICT_DAYS_BACK", "30"))
MODEL_VERSION = os.getenv("MODEL_VERSION", "ladder-catboost-20260716")

ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts"
FAMILY_DIR = ARTIFACT_DIR / "families"
MODEL_PATH = FAMILY_DIR / "ladder_3v3.catboost.cbm"
CALIBRATOR_PATH = FAMILY_DIR / "ladder_3v3.catboost.platt.joblib"
META_PATH = FAMILY_DIR / "ladder_3v3.catboost.json"

# Must match training scripts exactly
CATEGORICAL = ["mode", "map_key", "team_comp_key", "enemy_comp_key"]
NUMERIC = [
    "diff_avg_player_total_trophies",
    "diff_std_player_total_trophies",
    "diff_avg_player_level",
    "diff_min_player_level",
    "diff_max_player_level",
    "diff_std_player_level",
    "diff_avg_brawler_power",
    "diff_min_brawler_power",
    "diff_max_brawler_power",
    "diff_avg_brawler_total_trophies",
    "diff_min_brawler_total_trophies",
    "diff_max_brawler_total_trophies",
    "diff_std_brawler_total_trophies",
    "diff_avg_brawler_rank_on_account",
    "diff_min_brawler_rank_on_account",
    "diff_max_brawler_rank_on_account",
    "diff_std_brawler_rank_on_account",
    "diff_pct_highest_player_brawler",
    "team_avg_player_total_trophies",
    "enemy_avg_player_total_trophies",
    "team_avg_player_level",
    "enemy_avg_player_level",
    "team_avg_brawler_power",
    "enemy_avg_brawler_power",
]


def get_conn():
    if not all([DB_HOST, DB_USER, DB_PASSWORD, DB_NAME]):
        raise RuntimeError("Some DB_* variables are missing in environment")
    return database_connect()


def load_predict_df(days_back: int) -> pd.DataFrame:
    # IMPORTANT: don't duplicate columns that are already in CATEGORICAL
    # CATEGORICAL includes mode + map_key, so do NOT include them in key_cols here.
    key_cols = ["battletime", "type", "duration", "player_tag"]
    feat_cols = CATEGORICAL + NUMERIC
    col_sql = ",\n      ".join(key_cols + feat_cols)

    sql = f"""
    select
      {col_sql}
    from public.battle_features_3v3
    where battletime >= now() - make_interval(days => %s)
      and team_comp_key is not null
      and enemy_comp_key is not null
      and type = 'ranked'
      and mode = any(%s)
      and team_size = 3
      and enemy_size = 3;
    """

    conn = get_conn()
    try:
        df = pd.read_sql(
            sql,
            conn,
            params=(days_back, ["brawlBall", "gemGrab", "heist", "hotZone", "knockout", "bounty", "wipeout"]),
        )
        # Safety: if anything upstream ever duplicates column labels, drop dupes
        df = df.loc[:, ~df.columns.duplicated()].copy()
        return df
    finally:
        conn.close()


def _enforce_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    for c in CATEGORICAL:
        if c in df.columns:
            df[c] = df[c].astype("string")

    for c in NUMERIC:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")

    num_cols = [c for c in NUMERIC if c in df.columns]
    df[num_cols] = df[num_cols].replace([np.inf, -np.inf], np.nan)
    return df


def _expected_input_columns_from_model(model) -> list:
    """
    Best-effort fallback if meta doesn't have expected_input_columns.
    Works with CalibratedClassifierCV + FrozenEstimator(pipe).
    """
    try:
        if hasattr(model, "calibrated_classifiers_") and model.calibrated_classifiers_:
            frozen = model.calibrated_classifiers_[0].estimator  # FrozenEstimator or pipeline
            est = getattr(frozen, "estimator", frozen)           # unwrap if FrozenEstimator
            if hasattr(est, "feature_names_in_"):
                return list(est.feature_names_in_)
    except Exception:
        pass
    return CATEGORICAL + NUMERIC


def _align_X(expected_cols: list, X: pd.DataFrame) -> pd.DataFrame:
    # Critical: avoid "cannot reindex ... duplicate labels"
    X = X.loc[:, ~X.columns.duplicated()].copy()

    # add missing columns (rare, but makes it robust)
    for c in expected_cols:
        if c not in X.columns:
            X[c] = np.nan

    # enforce exact order and drop extras
    return X.reindex(columns=expected_cols)


def _safe_z(val: float, mean: float, std: float) -> float:
    if std is None or std == 0.0 or not np.isfinite(std):
        return 0.0
    z = (val - mean) / std
    if not np.isfinite(z):
        return 0.0
    return float(z)


NEG_IS_GOOD = {
    "diff_std_player_level",
    "diff_std_player_total_trophies",
    "diff_std_brawler_total_trophies",
    "diff_std_brawler_rank_on_account",
}

PLAYER_LABELS = {
    "diff_avg_player_level": "Average player level",
    "diff_avg_brawler_power": "Average brawler power",
    "diff_avg_brawler_total_trophies": "Average brawler trophies",
    "diff_min_player_level": "Lowest player level",
    "diff_max_player_level": "Highest player level",
    "diff_max_brawler_power": "Highest brawler power",
    "diff_std_player_level": "Team balance (level spread)",
    "diff_std_brawler_rank_on_account": "Consistency (rank spread)",
    "diff_avg_player_total_trophies": "Average account trophies",
}

TECH_LABELS = {k: k for k in NUMERIC}

EXPLANATION_GROUPS = {
    "brawler_strength": {
        "label": "Brawler strength",
        "features": {"diff_avg_brawler_power", "diff_min_brawler_power", "diff_max_brawler_power"},
    },
    "account_experience": {
        "label": "Account experience",
        "features": {"diff_avg_player_total_trophies", "team_avg_player_total_trophies", "enemy_avg_player_total_trophies", "diff_avg_player_level", "diff_min_player_level", "diff_max_player_level", "team_avg_player_level", "enemy_avg_player_level"},
    },
    "team_balance": {
        "label": "Team balance",
        "features": {"diff_std_player_total_trophies", "diff_std_player_level", "diff_std_brawler_total_trophies", "diff_std_brawler_rank_on_account"},
    },
    "brawler_progression": {
        "label": "Brawler progression",
        "features": {"diff_avg_brawler_total_trophies", "diff_min_brawler_total_trophies", "diff_max_brawler_total_trophies", "diff_avg_brawler_rank_on_account", "diff_min_brawler_rank_on_account", "diff_max_brawler_rank_on_account", "diff_pct_highest_player_brawler", "team_avg_brawler_power", "enemy_avg_brawler_power"},
    },
    "mode_map_context": {"label": "Mode and map context", "features": {"mode", "map_key"}},
    "composition_matchup": {"label": "Composition matchup", "features": {"team_comp_key", "enemy_comp_key"}},
}


def _json_value(value):
    if pd.isna(value):
        return None
    if isinstance(value, np.generic):
        return value.item()
    return value


def build_shap_explanations(model, X, raw_probs, calibrated_probs, final_probs):
    """Return model-faithful, grouped explanations in CatBoost raw-score space."""
    shap_pool = Pool(X, cat_features=CATEGORICAL)
    shap = np.asarray(model.get_feature_importance(shap_pool, type="ShapValues"), dtype=float)
    feature_names = list(X.columns)
    feature_values = shap[:, :-1]
    baselines = shap[:, -1]
    feature_to_group = {
        feature: (key, spec["label"])
        for key, spec in EXPLANATION_GROUPS.items()
        for feature in spec["features"]
    }
    explanations = []
    for row_idx, (_, row) in enumerate(X.iterrows()):
        grouped = {}
        raw_features = []
        for col_idx, feature in enumerate(feature_names):
            contribution = float(feature_values[row_idx, col_idx])
            group_key, group_label = feature_to_group.get(feature, ("other", "Other model context"))
            grouped.setdefault(group_key, {"key": group_key, "label": group_label, "raw_score_contribution": 0.0, "features": []})
            grouped[group_key]["raw_score_contribution"] += contribution
            grouped[group_key]["features"].append({
                "feature": feature,
                "value": _json_value(row[feature]),
                "raw_score_contribution": round(contribution, 6),
            })
            raw_features.append(grouped[group_key]["features"][-1])
        factors = sorted(grouped.values(), key=lambda item: abs(item["raw_score_contribution"]), reverse=True)
        total_movement = sum(abs(item["raw_score_contribution"]) for item in factors) or 1.0
        for factor in factors:
            factor["raw_score_contribution"] = round(factor["raw_score_contribution"], 6)
            factor["direction"] = "raised" if factor["raw_score_contribution"] > 0 else "lowered"
            factor["movement_share_pct"] = round(abs(factor["raw_score_contribution"]) * 100.0 / total_movement)
            factor["features"] = sorted(factor["features"], key=lambda item: abs(item["raw_score_contribution"]), reverse=True)
        material = [factor for factor in factors if abs(factor["raw_score_contribution"]) >= 0.02]
        positives = [factor for factor in material if factor["raw_score_contribution"] > 0][:2]
        negatives = [factor for factor in material if factor["raw_score_contribution"] < 0][:2]
        if positives or negatives:
            clauses = []
            if positives:
                clauses.append(" and ".join(factor["label"] for factor in positives) + " raised the estimate")
            if negatives:
                clauses.append(" and ".join(factor["label"] for factor in negatives) + " lowered it")
            summary = "; ".join(clauses) + "."
        else:
            summary = "No single factor strongly moved this balanced matchup."
        explanations.append({
            "schema_version": 2,
            "model_version": MODEL_VERSION,
            "method": "catboost_shap_raw_score",
            "baseline_raw_score": round(float(baselines[row_idx]), 6),
            "raw_model_probability": round(float(raw_probs[row_idx]), 8),
            "calibrated_probability": round(float(calibrated_probs[row_idx]), 8),
            "final_probability": round(float(final_probs[row_idx]), 8),
            "calibration_adjustment": round(float(calibrated_probs[row_idx] - raw_probs[row_idx]), 8),
            "reciprocal_adjustment": round(float(final_probs[row_idx] - calibrated_probs[row_idx]), 8),
            "summary": summary,
            "positive_factors": positives,
            "negative_factors": negatives,
            "all_groups": factors,
            "feature_contributions": raw_features,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "disclaimer": "Explains the model estimate, not what caused the battle result.",
        })
    return explanations


def build_explanations(row: pd.Series, numeric_stats: dict) -> tuple[str, str]:
    drivers = []

    for feat in [
        "diff_std_player_level",
        "diff_std_brawler_rank_on_account",
        "diff_avg_brawler_total_trophies",
        "diff_avg_player_level",
        "diff_min_player_level",
        "diff_avg_brawler_power",
        "diff_max_player_level",
        "diff_max_brawler_power",
        "diff_max_brawler_total_trophies",
    ]:
        if feat not in row.index:
            continue

        val = float(row.get(feat, 0.0))
        st = (numeric_stats or {}).get(feat, {})
        mu = float(st.get("mean", 0.0))
        sd = float(st.get("std", 0.0))
        z = _safe_z(val, mu, sd)

        adv = -z if feat in NEG_IS_GOOD else z
        drivers.append({"feat": feat, "val": val, "z": z, "adv": adv})

    if not drivers:
        return ("No drivers available.", "No strong drivers detected.")

    drivers_sorted = sorted(drivers, key=lambda d: abs(d["adv"]), reverse=True)
    top = drivers_sorted[:4]

    pros = [d for d in top if d["adv"] > 0.15]
    cons = [d for d in top if d["adv"] < -0.15]

    tech_parts = []
    for d in top:
        feat = d["feat"]
        sign = "+" if d["adv"] >= 0 else "-"
        note = " (lower spread better)" if feat in NEG_IS_GOOD else ""
        tech_parts.append(
            f"{sign} {TECH_LABELS.get(feat, feat)}: adv={d['adv']:+.2f} z={d['z']:+.2f} raw={d['val']:+.2f}{note}"
        )
    tech = " | ".join(tech_parts)

    def _phrase(d):
        label = PLAYER_LABELS.get(d["feat"], d["feat"])
        if d["feat"] in NEG_IS_GOOD:
            return f"{label} looks better ({d['val']:+.2f})"
        return f"{label} advantage ({d['val']:+.2f})"

    def _phrase_con(d):
        label = PLAYER_LABELS.get(d["feat"], d["feat"])
        if d["feat"] in NEG_IS_GOOD:
            return f"{label} looks worse ({d['val']:+.2f})"
        return f"{label} disadvantage ({d['val']:+.2f})"

    if not pros and not cons:
        player = "Teams look pretty even — no strong edge detected."
    else:
        parts = []
        if pros:
            parts.append("Good signs: " + "; ".join(_phrase(d) for d in pros[:2]))
        if cons:
            parts.append("Watch out: " + "; ".join(_phrase_con(d) for d in cons[:2]))
        player = " | ".join(parts)

    return tech, player


def enforce_mirrored_probabilities(df: pd.DataFrame, probs: np.ndarray) -> np.ndarray:
    """Make reciprocal team views sum to exactly one when both are present."""
    def stable(value):
        return None if pd.isna(value) else value

    result = np.asarray(probs, dtype=float).copy()
    lookup = {}
    for idx, row in df.reset_index(drop=True).iterrows():
        key = (
            stable(row["battletime"]), stable(row["mode"]), stable(row["map_key"]),
            stable(row["type"]), stable(row["duration"]),
            row["team_comp_key"], row["enemy_comp_key"],
        )
        lookup.setdefault(key, []).append(idx)

    done = set()
    for key, indices in lookup.items():
        reciprocal = key[:5] + (key[6], key[5])
        other = lookup.get(reciprocal)
        pair_id = frozenset((key, reciprocal))
        if not other or pair_id in done:
            continue
        p = float(np.mean(result[indices]))
        q = float(np.mean(result[other]))
        symmetric = (p + (1.0 - q)) / 2.0
        result[indices] = symmetric
        result[other] = 1.0 - symmetric
        done.add(pair_id)
    return result


def main():
    t0 = time.time()
    print(f"[predict] start | PREDICT_DAYS_BACK={PREDICT_DAYS_BACK} | version={MODEL_VERSION}")

    if not MODEL_PATH.exists():
        raise RuntimeError(f"Model not found: {MODEL_PATH}. Run train first.")
    if not CALIBRATOR_PATH.exists():
        raise RuntimeError(f"Calibrator not found: {CALIBRATOR_PATH}. Run the family challenger first.")

    meta = json.loads(META_PATH.read_text()) if META_PATH.exists() else {}
    model = CatBoostClassifier()
    model.load_model(str(MODEL_PATH))
    calibrator = joblib.load(CALIBRATOR_PATH)
    expected_cols = CATEGORICAL + NUMERIC

    df = load_predict_df(PREDICT_DAYS_BACK)
    if df.empty:
        print("[predict] no rows to predict")
        return

    df = _enforce_dtypes(df)

    # Keys for the UPDATE where-clause (mode/map_key exist in df via CATEGORICAL)
    key_cols = ["battletime", "mode", "map_key", "type", "duration", "player_tag"]

    X = df[CATEGORICAL + NUMERIC].copy()
    X = _align_X(expected_cols, X)
    X = _enforce_dtypes(X)  # important after reindex

    for col in CATEGORICAL:
        X[col] = X[col].fillna("UNKNOWN").astype(str)
    raw_probs = model.predict_proba(X)[:, 1]
    eps = 1e-6
    logits = np.log(
        np.clip(raw_probs, eps, 1 - eps) / np.clip(1 - raw_probs, eps, 1 - eps)
    ).reshape(-1, 1)
    calibrated_probs = calibrator.predict_proba(logits)[:, 1]
    probs = enforce_mirrored_probabilities(df, calibrated_probs)
    explanations = build_shap_explanations(model, X, raw_probs, calibrated_probs, probs)

    out = df[key_cols].copy()
    out["pred_win_prob"] = probs.astype(float)

    serialized_explanations = [json.dumps(item, separators=(",", ":"), allow_nan=False) for item in explanations]
    out["pred_explanation"] = serialized_explanations
    out["pred_explanation_tech"] = serialized_explanations
    out["pred_explanation_player"] = [item["summary"] for item in explanations]

    out["model_version"] = MODEL_VERSION
    out["pred_updated_at"] = dt.datetime.utcnow()

    update_sql = """
    update public.battle_features_3v3
    set
      pred_win_prob = %(pred_win_prob)s,
      pred_explanation_tech = %(pred_explanation_tech)s,
      pred_explanation_player = %(pred_explanation_player)s,
      pred_explanation = %(pred_explanation)s::jsonb,
      model_version = %(model_version)s,
      pred_updated_at = %(pred_updated_at)s
    where battletime = %(battletime)s
      and mode = %(mode)s
      and map_key = %(map_key)s
      and type is not distinct from %(type)s
      and duration is not distinct from %(duration)s
      and player_tag = %(player_tag)s;
    """

    rows = out.to_dict(orient="records")

    conn = get_conn()
    cur = conn.cursor()
    try:
        # The product contract is explicit: only the validated Ladder family is
        # live. Remove stale universal-model probabilities from every other
        # context in the same window before writing Ladder predictions.
        cur.execute(
            """
            update public.battle_features_3v3
               set pred_win_prob = null,
                   pred_explanation_tech = null,
                   pred_explanation_player = null,
                   pred_explanation = null,
                   model_version = null,
                   pred_updated_at = null
             where battletime >= now() - make_interval(days => %s)
            """,
            (PREDICT_DAYS_BACK,),
        )
        execute_batch(cur, update_sql, rows, page_size=500)
        conn.commit()
    finally:
        cur.close()
        conn.close()

    print(f"[predict] done | rows_updated={len(rows)} | {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
