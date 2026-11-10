#!/usr/bin/env python3
import os
import json
import time
import datetime as dt
from pathlib import Path
import warnings

import numpy as np
import psycopg2
from database import connect as database_connect
import pandas as pd
from dotenv import load_dotenv

from sklearn.pipeline import Pipeline
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import OneHotEncoder, StandardScaler, FunctionTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import roc_auc_score, log_loss, brier_score_loss
from sklearn.inspection import permutation_importance
from sklearn.frozen import FrozenEstimator
from scipy import sparse

import joblib
from ml_utils import sanitize_matrix

# Load .env (repo root)
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")

TRAIN_DAYS_BACK = int(os.getenv("TRAIN_DAYS_BACK", "120"))
MIN_TEAM_SIZE = int(os.getenv("TRAIN_MIN_TEAM_SIZE", "3"))
MODEL_VERSION = os.getenv("MODEL_VERSION", os.getenv("GITHUB_SHA", "local"))

ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts"
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

MODEL_PATH = ARTIFACT_DIR / "victory_model.joblib"
META_PATH = ARTIFACT_DIR / "victory_model_meta.json"
REPORT_JSON_PATH = ARTIFACT_DIR / "train_report.json"
REPORT_TXT_PATH = ARTIFACT_DIR / "train_report.txt"

CALIB_BINS_PATH = ARTIFACT_DIR / "calibration_bins.csv"
COEF_IMPORTANCE_PATH = ARTIFACT_DIR / "feature_importance_coeffs.csv"
PERM_IMPORTANCE_PATH = ARTIFACT_DIR / "feature_importance_permutation.csv"
GROUPED_IMPORTANCE_PATH = ARTIFACT_DIR / "feature_importance_grouped.csv"

# Optional: suppress only the sklearn extmath matmul runtime warnings (noise)
warnings.filterwarnings(
    "ignore",
    message=r".*encountered in matmul.*",
    category=RuntimeWarning,
    module=r"sklearn\.utils\.extmath",
)


def get_conn():
    if not all([DB_HOST, DB_USER, DB_PASSWORD, DB_NAME]):
        raise RuntimeError("Some DB_* variables are missing in environment")
    return database_connect()


CATEGORICAL = ["mode", "map_key", "team_comp_key", "enemy_comp_key", "matchup_key"]

NUMERIC = [
    "team_size",
    "enemy_size",
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

# Baseline features (used for baseline AUC comparisons)
BASELINE_TROPHY = "diff_avg_player_total_trophies"
BASELINE_PWR = "diff_avg_brawler_power"
BASELINE_RANK = "diff_avg_brawler_rank_on_account"


def load_training_df(days_back: int) -> pd.DataFrame:
    cols = ["battletime", "win_label"] + CATEGORICAL + NUMERIC
    col_sql = ",\n      ".join(cols)

    sql = f"""
    select
      {col_sql}
    from public.battle_features_3v3
    where battletime >= now() - make_interval(days => %s)
      and win_label in (0,1)
      and mode not in ('soloShowdown','duoShowdown')
      and team_size >= %s
      and enemy_size >= %s
      and team_comp_key is not null
      and enemy_comp_key is not null
      and matchup_key is not null;
    """

    conn = get_conn()
    try:
        return pd.read_sql(sql, conn, params=(days_back, MIN_TEAM_SIZE, MIN_TEAM_SIZE))
    finally:
        conn.close()


def _enforce_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    # categorical -> string (keeps OneHot stable)
    for c in CATEGORICAL:
        if c in df.columns:
            df[c] = df[c].astype("string")

    # numeric -> float64
    for c in NUMERIC:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")

    return df

def compute_numeric_stats(df: pd.DataFrame) -> dict:
    """
    Compute mean/std for NUMERIC columns for z-score explanations at predict time.
    Uses float64, ignores NaNs.
    """
    stats = {}
    for c in NUMERIC:
        s = pd.to_numeric(df[c], errors="coerce").astype("float64")
        mu = float(s.mean(skipna=True))
        sd = float(s.std(skipna=True, ddof=0))  # population std, stable
        # avoid sd=0 later
        stats[c] = {"mean": mu, "std": sd}
    return stats

def _clean_numeric_df(df: pd.DataFrame, numeric_cols: list) -> pd.DataFrame:
    for c in numeric_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df[numeric_cols] = df[numeric_cols].replace([np.inf, -np.inf], np.nan)

    # winsorize to robust quantiles
    for c in numeric_cols:
        s = df[c]
        if s.notna().sum() >= 50:
            lo = s.quantile(0.001)
            hi = s.quantile(0.999)
            df[c] = s.clip(lower=lo, upper=hi)

    # final hard clip
    HARD_CLIP = 1e6
    df[numeric_cols] = df[numeric_cols].clip(lower=-HARD_CLIP, upper=HARD_CLIP)
    return df


def _drop_nonfinite_rows(df: pd.DataFrame) -> pd.DataFrame:
    mask = np.isfinite(df[NUMERIC].to_numpy(dtype="float64")).all(axis=1)
    return df.loc[mask].copy()


def _group_from_feature_name(feat_name: str) -> str:
    if not feat_name:
        return "other"

    if feat_name.startswith("num__"):
        base = feat_name[len("num__") :]
        if base.startswith("team_"):
            return "team_numeric"
        if base.startswith("enemy_"):
            return "enemy_numeric"
        if base.startswith("diff_"):
            return "diff_numeric"
        return "num_other"

    if feat_name.startswith("cat__"):
        base = feat_name[len("cat__") :]
        if base.startswith("matchup_key_"):
            return "matchup_key"
        if base.startswith("team_comp_key_"):
            return "team_comp_key"
        if base.startswith("enemy_comp_key_"):
            return "enemy_comp_key"
        if base.startswith("map_key_") or base.startswith("map_") or base.startswith("map_id_"):
            return "map"
        if base.startswith("mode_"):
            return "mode"
        return "cat_other"

    return "other"


def make_calibration_bins(y_true: np.ndarray, p: np.ndarray, n_bins: int = 10) -> pd.DataFrame:
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    bin_id = np.digitize(p, edges, right=True) - 1
    bin_id = np.clip(bin_id, 0, n_bins - 1)

    df = pd.DataFrame({"y": y_true.astype(int), "p": p.astype(float), "bin": bin_id})
    g = df.groupby("bin", as_index=False).agg(
        n=("y", "size"),
        avg_pred=("p", "mean"),
        win_rate=("y", "mean"),
    )
    g["abs_gap"] = (g["avg_pred"] - g["win_rate"]).abs()
    g["bin_lo"] = edges[g["bin"].values]
    g["bin_hi"] = edges[g["bin"].values + 1]
    g = g[["bin", "bin_lo", "bin_hi", "n", "avg_pred", "win_rate", "abs_gap"]]

    total = g["n"].sum()
    ece = float((g["n"] / total * g["abs_gap"]).sum()) if total > 0 else float("nan")
    g.attrs["ece"] = ece
    return g


def baseline_auc(y_true: np.ndarray, score_like) -> float:
    """
    Baseline AUC using a raw numeric score.
    AUC is rank-based; no scaling/fitting needed.
    """
    s = pd.to_numeric(score_like, errors="coerce")
    s = s.fillna(s.median())
    return float(roc_auc_score(y_true, s))


def write_text_report(meta: dict) -> None:
    m = meta["metrics"]
    lines = [
        "Victory model training report",
        "============================",
        f"model_version: {meta['model_version']}",
        f"trained_at:    {meta['trained_at']}",
        f"rows:         {meta['rows']}",
        f"train_days:   {meta['train_days_back']}",
        "",
        "Metrics (test set):",
        f"  ROC AUC : {m['roc_auc']:.6f}",
        f"  LogLoss : {m['log_loss']:.6f}",
        f"  Brier   : {m['brier']:.6f}",
        f"  ECE(10) : {m.get('ece_10bin', float('nan')):.6f}",
        "",
        "Baseline AUC (test set):",
        f"  trophy_diff     : {m.get('baseline_auc', {}).get('trophy_diff', float('nan')):.6f}",
        f"  brawler_power   : {m.get('baseline_auc', {}).get('brawler_power_diff', float('nan')):.6f}",
        f"  simple_combo    : {m.get('baseline_auc', {}).get('simple_combo', float('nan')):.6f}",
        "",
        "AUC lift vs baseline:",
        f"  vs trophy_diff  : {m.get('auc_lift_vs_baseline', {}).get('trophy_diff', float('nan')):.6f}",
        f"  vs brawler_power: {m.get('auc_lift_vs_baseline', {}).get('brawler_power_diff', float('nan')):.6f}",
        f"  vs simple_combo : {m.get('auc_lift_vs_baseline', {}).get('simple_combo', float('nan')):.6f}",
        "",
        "Outputs:",
        f"  {MODEL_PATH.name}",
        f"  {META_PATH.name}",
        f"  {REPORT_JSON_PATH.name}",
        f"  {REPORT_TXT_PATH.name}",
        f"  {CALIB_BINS_PATH.name}",
        f"  {COEF_IMPORTANCE_PATH.name}",
        f"  {PERM_IMPORTANCE_PATH.name}",
        f"  {GROUPED_IMPORTANCE_PATH.name}",
    ]
    REPORT_TXT_PATH.write_text("\n".join(lines) + "\n")


def main():
    t0 = time.time()
    print(f"[train] start | TRAIN_DAYS_BACK={TRAIN_DAYS_BACK} | version={MODEL_VERSION}")

    df = load_training_df(TRAIN_DAYS_BACK)
    if df.empty:
        raise RuntimeError("No training rows returned. Run refresh first, or increase TRAIN_DAYS_BACK.")

    df = _enforce_dtypes(df)
    df = _clean_numeric_df(df, NUMERIC)
    df = _drop_nonfinite_rows(df)

    numeric_stats = compute_numeric_stats(df)

    df = df.sort_values("battletime", kind="stable").reset_index(drop=True)
    train_end = int(len(df) * 0.70)
    calibration_end = int(len(df) * 0.85)
    if train_end < 100 or calibration_end <= train_end or calibration_end >= len(df):
        raise RuntimeError("Not enough rows for chronological train/calibration/test windows.")

    train_df = df.iloc[:train_end]
    calibration_df = df.iloc[train_end:calibration_end]
    test_df = df.iloc[calibration_end:]
    for name, split in (("train", train_df), ("calibration", calibration_df), ("test", test_df)):
        if split["win_label"].nunique() < 2:
            raise RuntimeError(f"Chronological {name} window contains only one class.")

    X_train = train_df.drop(columns=["battletime", "win_label"])
    y_train = train_df["win_label"].astype(int).to_numpy()
    X_cal = calibration_df.drop(columns=["battletime", "win_label"])
    y_cal = calibration_df["win_label"].astype(int).to_numpy()
    X_test = test_df.drop(columns=["battletime", "win_label"])
    y_test = test_df["win_label"].astype(int).to_numpy()

    pre = ColumnTransformer(
        transformers=[
            (
                "num",
                Pipeline(
                    steps=[
                        ("imputer", SimpleImputer(strategy="median")),
                        ("scaler", StandardScaler(with_mean=False)),
                    ]
                ),
                NUMERIC,
            ),
            (
                "cat",
                Pipeline(
                    steps=[
                        ("imputer", SimpleImputer(strategy="most_frequent")),
                        ("onehot", OneHotEncoder(handle_unknown="ignore", min_frequency=50, dtype=np.float64)),
                    ]
                ),
                CATEGORICAL,
            ),
        ],
        remainder="drop",
    )

    base = LogisticRegression(
        solver="liblinear",
        max_iter=4000,
        class_weight="balanced",
        C=0.3,
        penalty="l2",
    )

    sanitize = FunctionTransformer(sanitize_matrix, accept_sparse=True)

    pipe = Pipeline(steps=[
        ("pre", pre),
        ("sanitize", sanitize),
        ("clf", base),
    ])

    pipe.fit(X_train, y_train)
    expected_cols = list(pipe.feature_names_in_)

    calibrated = CalibratedClassifierCV(FrozenEstimator(pipe), method="sigmoid", cv=5)
    calibrated.fit(X_cal, y_cal)

    p = calibrated.predict_proba(X_test)[:, 1]
    p = np.nan_to_num(p, nan=0.5, posinf=1.0, neginf=0.0)

    auc = float(roc_auc_score(y_test, p))
    ll = float(log_loss(y_test, p, labels=[0, 1]))
    brier = float(brier_score_loss(y_test, p))

    # --- Baseline AUCs on SAME test set ---
    baseline_results = {}

    if BASELINE_TROPHY in X_test.columns:
        baseline_results["trophy_diff"] = baseline_auc(y_test, X_test[BASELINE_TROPHY])
    else:
        baseline_results["trophy_diff"] = float("nan")

    if BASELINE_PWR in X_test.columns:
        baseline_results["brawler_power_diff"] = baseline_auc(y_test, X_test[BASELINE_PWR])
    else:
        baseline_results["brawler_power_diff"] = float("nan")

    # Simple combo baseline (sum of 3 diffs)
    combo_cols = [c for c in [BASELINE_TROPHY, BASELINE_PWR, BASELINE_RANK] if c in X_test.columns]
    if combo_cols:
        baseline_results["simple_combo"] = baseline_auc(y_test, X_test[combo_cols].sum(axis=1))
    else:
        baseline_results["simple_combo"] = float("nan")

    auc_lift = {k: (auc - v) if (v == v) else float("nan") for k, v in baseline_results.items()}  # v==v checks not-NaN

    # Calibration bins
    bins = make_calibration_bins(y_test, p, n_bins=10)
    bins.to_csv(CALIB_BINS_PATH, index=False)
    ece_10 = float(bins.attrs.get("ece", float("nan")))

    # Coefficient importances
    fitted_pipe = calibrated.calibrated_classifiers_[0].estimator
    expanded_names = fitted_pipe.named_steps["pre"].get_feature_names_out()
    coefs = fitted_pipe.named_steps["clf"].coef_.ravel()

    coef_df = pd.DataFrame({
        "feature": expanded_names,
        "coef": coefs,
        "abs_coef": np.abs(coefs),
        "group": [_group_from_feature_name(n) for n in expanded_names],
    }).sort_values("abs_coef", ascending=False)
    coef_df.to_csv(COEF_IMPORTANCE_PATH, index=False)

    # Permutation importance (n_jobs=1 prevents joblib worker warnings bleed-through)
    perm = permutation_importance(
        calibrated,
        X_test,
        y_test,
        n_repeats=5,
        random_state=42,
        scoring="neg_log_loss",
        n_jobs=1,
    )

    perm_df = pd.DataFrame({
        "feature": list(X_test.columns),
        "importance_mean": perm.importances_mean,
        "importance_std": perm.importances_std,
    }).sort_values("importance_mean", ascending=False)
    perm_df.to_csv(PERM_IMPORTANCE_PATH, index=False)

    grouped = (
        coef_df.groupby("group", as_index=False)
        .agg(
            importance_mean=("abs_coef", "sum"),
            expanded_features=("feature", "count"),
        )
        .sort_values("importance_mean", ascending=False)
    )
    grouped.to_csv(GROUPED_IMPORTANCE_PATH, index=False)

    meta = {
        "model_version": MODEL_VERSION,
        "trained_at": dt.datetime.utcnow().isoformat() + "Z",
        "train_days_back": TRAIN_DAYS_BACK,
        "rows": int(len(df)),
        "splits": {"train": int(len(X_train)), "calibration": int(len(X_cal)), "test": int(len(X_test))},
        "split_strategy": "chronological_70_15_15",
        "split_ranges": {
            "train": [train_df["battletime"].min().isoformat(), train_df["battletime"].max().isoformat()],
            "calibration": [calibration_df["battletime"].min().isoformat(), calibration_df["battletime"].max().isoformat()],
            "test": [test_df["battletime"].min().isoformat(), test_df["battletime"].max().isoformat()],
        },
        "metrics": {
            "roc_auc": auc,
            "log_loss": ll,
            "brier": brier,
            "ece_10bin": ece_10,
            "baseline_auc": baseline_results,
            "auc_lift_vs_baseline": {k: float(v) for k, v in auc_lift.items()},
        },
        "features": {"categorical": CATEGORICAL, "numeric": NUMERIC},
        "numeric_stats": numeric_stats,
        "expected_input_columns": expected_cols,
        "outputs": {
            "model": str(MODEL_PATH),
            "meta": str(META_PATH),
            "report_json": str(REPORT_JSON_PATH),
            "report_txt": str(REPORT_TXT_PATH),
            "calibration_bins_csv": str(CALIB_BINS_PATH),
            "coef_importance_csv": str(COEF_IMPORTANCE_PATH),
            "permutation_importance_csv": str(PERM_IMPORTANCE_PATH),
            "grouped_importance_csv": str(GROUPED_IMPORTANCE_PATH),
        },
        "top_features_preview": {
            "grouped_coef_top10": grouped.head(10).to_dict(orient="records"),
            "perm_top10": perm_df.head(10).to_dict(orient="records"),
            "coef_top10": coef_df.head(10).to_dict(orient="records"),
        },
    }

    joblib.dump(calibrated, MODEL_PATH)
    META_PATH.write_text(json.dumps(meta, indent=2))
    REPORT_JSON_PATH.write_text(json.dumps(meta, indent=2))
    write_text_report(meta)

    print(f"[train] saved model -> {MODEL_PATH}")
    print(f"[train] saved meta  -> {META_PATH}")
    print(f"[train] saved report -> {REPORT_JSON_PATH} and {REPORT_TXT_PATH}")
    print(f"[train] saved calibration -> {CALIB_BINS_PATH}")
    print(f"[train] saved feature relevance -> {COEF_IMPORTANCE_PATH}, {PERM_IMPORTANCE_PATH}, {GROUPED_IMPORTANCE_PATH}")
    print(
        f"[train] done | rows={len(df)} auc={auc:.4f} "
        f"(baseline trophy={baseline_results.get('trophy_diff', float('nan')):.4f}, "
        f"lift={auc_lift.get('trophy_diff', float('nan')):.4f}) "
        f"logloss={ll:.4f} brier={brier:.4f} ece10={ece_10:.4f} | {time.time()-t0:.1f}s"
    )


if __name__ == "__main__":
    main()
