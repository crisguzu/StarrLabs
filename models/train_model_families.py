#!/usr/bin/env python3
"""Train context-specific, chronologically evaluated BSA model families."""

import datetime as dt
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.compose import ColumnTransformer
from sklearn.frozen import FrozenEstimator
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from database import connect


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "artifacts" / "families"
OUT.mkdir(parents=True, exist_ok=True)
CORE_3V3_MODES = ("brawlBall", "gemGrab", "heist", "hotZone", "knockout", "bounty", "wipeout")
RANKED_3V3_MODES = CORE_3V3_MODES[:-1]

TEAM_CATEGORICAL = ["mode", "map_key", "team_comp_key", "enemy_comp_key"]
TEAM_NUMERIC = [
    "diff_avg_player_total_trophies", "diff_std_player_total_trophies",
    "diff_avg_player_level", "diff_min_player_level", "diff_max_player_level", "diff_std_player_level",
    "diff_avg_brawler_power", "diff_min_brawler_power", "diff_max_brawler_power",
    "diff_avg_brawler_total_trophies", "diff_min_brawler_total_trophies",
    "diff_max_brawler_total_trophies", "diff_std_brawler_total_trophies",
    "diff_avg_brawler_rank_on_account", "diff_min_brawler_rank_on_account",
    "diff_max_brawler_rank_on_account", "diff_std_brawler_rank_on_account",
    "diff_pct_highest_player_brawler",
    "team_avg_player_total_trophies", "enemy_avg_player_total_trophies",
    "team_avg_player_level", "enemy_avg_player_level",
    "team_avg_brawler_power", "enemy_avg_brawler_power",
]
SHOWDOWN_CATEGORICAL = ["mode", "map_key", "battle_type", "brawler_name"]
SHOWDOWN_NUMERIC = [
    "player_total_trophies", "player_level", "brawler_power",
    "brawler_total_trophies", "brawler_rank_on_account",
    "player_success_last10", "player_success_last25", "player_rank_last10",
    "player_brawler_success_last10", "player_brawler_rank_last10",
    "player_history_count", "player_brawler_history_count",
]


def load_team_family(battle_type: str, modes: tuple[str, ...]) -> pd.DataFrame:
    cols = ["battletime", "type", "duration", "win_label"] + TEAM_CATEGORICAL + TEAM_NUMERIC
    sql = f"""
      SELECT {', '.join(cols)}
      FROM battle_features_3v3
      WHERE battletime >= now() - interval '120 days'
        AND type = %s AND mode = ANY(%s)
        AND team_size = 3 AND enemy_size = 3 AND win_label IN (0,1)
        AND team_comp_key IS NOT NULL AND enemy_comp_key IS NOT NULL
      ORDER BY battletime
    """
    with connect() as conn:
        df = pd.read_sql(sql, conn, params=(battle_type, list(modes)))
    # All players on one team have the same pre-battle team features and label.
    # Keep one directed team row so each match contributes two symmetric examples.
    return df.drop_duplicates(
        ["battletime", "mode", "map_key", "type", "duration", "team_comp_key", "enemy_comp_key", "win_label"]
    ).reset_index(drop=True)


def load_showdown_family(mode: str, success_max_rank: int) -> pd.DataFrame:
    sql = """
      SELECT battletime_ts AS battletime, mode, COALESCE(map_id::text, map) AS map_key,
             type AS battle_type, player_tag, brawler_name, rank,
             player_total_trophies, player_level, brawler_power,
             brawler_total_trophies, brawler_rank_on_account
      FROM battles
      WHERE battletime_ts >= now() - interval '120 days'
        AND mode = %s AND rank IS NOT NULL
      ORDER BY battletime_ts
    """
    with connect() as conn:
        df = pd.read_sql(sql, conn, params=(mode,))
    df = df.drop_duplicates(["battletime", "mode", "map_key", "battle_type", "player_tag"])
    df["success_label"] = (df["rank"] <= success_max_rank).astype(int)
    # Strictly prior observed-player history. shift() prevents the current result
    # from entering its own features; sparse/new players remain missing and are imputed.
    by_player = df.groupby("player_tag", sort=False, group_keys=False)
    df["player_success_last10"] = by_player["success_label"].transform(
        lambda s: s.shift().rolling(10, min_periods=2).mean()
    )
    df["player_success_last25"] = by_player["success_label"].transform(
        lambda s: s.shift().rolling(25, min_periods=3).mean()
    )
    df["player_rank_last10"] = by_player["rank"].transform(
        lambda s: s.shift().rolling(10, min_periods=2).mean()
    )
    df["player_history_count"] = by_player.cumcount()
    by_player_brawler = df.groupby(["player_tag", "brawler_name"], sort=False, group_keys=False)
    df["player_brawler_success_last10"] = by_player_brawler["success_label"].transform(
        lambda s: s.shift().rolling(10, min_periods=2).mean()
    )
    df["player_brawler_rank_last10"] = by_player_brawler["rank"].transform(
        lambda s: s.shift().rolling(10, min_periods=2).mean()
    )
    df["player_brawler_history_count"] = by_player_brawler.cumcount()
    return df.reset_index(drop=True)


def estimator(categorical, numeric):
    pre = ColumnTransformer([
        ("num", Pipeline([("impute", SimpleImputer(strategy="median")), ("scale", StandardScaler())]), numeric),
        ("cat", Pipeline([("impute", SimpleImputer(strategy="most_frequent")),
                          ("onehot", OneHotEncoder(handle_unknown="ignore", min_frequency=10))]), categorical),
    ])
    return Pipeline([("pre", pre), ("clf", LogisticRegression(C=0.3, max_iter=4000, solver="liblinear"))])


def metrics(y, p):
    return {
        "roc_auc": float(roc_auc_score(y, p)),
        "brier": float(brier_score_loss(y, p)),
        "log_loss": float(log_loss(y, p, labels=[0, 1])),
        "actual_rate": float(np.mean(y)),
        "predicted_rate": float(np.mean(p)),
    }


def train_family(name, df, target, categorical, numeric, contract):
    df = df.sort_values("battletime", kind="stable").reset_index(drop=True)
    n = len(df); train_end = int(n * .70); cal_end = int(n * .85)
    if train_end < 500 or cal_end >= n:
        raise RuntimeError(f"{name}: insufficient rows ({n})")
    train, cal, test = df.iloc[:train_end], df.iloc[train_end:cal_end], df.iloc[cal_end:]
    features = categorical + numeric
    model = estimator(categorical, numeric)
    model.fit(train[features], train[target].astype(int))
    calibrated = CalibratedClassifierCV(FrozenEstimator(model), method="sigmoid")
    calibrated.fit(cal[features], cal[target].astype(int))
    p = calibrated.predict_proba(test[features])[:, 1]
    report = {
        "family": name, "contract": contract, "rows": n,
        "splits": {"train": len(train), "calibration": len(cal), "test": len(test)},
        "ranges": {
            "train": [train.battletime.min().isoformat(), train.battletime.max().isoformat()],
            "calibration": [cal.battletime.min().isoformat(), cal.battletime.max().isoformat()],
            "test": [test.battletime.min().isoformat(), test.battletime.max().isoformat()],
        },
        "metrics": metrics(test[target].astype(int).to_numpy(), p),
        "features": {"categorical": categorical, "numeric": numeric},
    }
    joblib.dump(calibrated, OUT / f"{name}.joblib")
    (OUT / f"{name}.json").write_text(json.dumps(report, indent=2))
    return report


def main():
    families = [
        ("ranked_3v3", load_team_family("soloRanked", RANKED_3V3_MODES), "win_label",
         TEAM_CATEGORICAL, TEAM_NUMERIC, {"type": "soloRanked", "team_size": 3, "modes": RANKED_3V3_MODES}),
        ("ladder_3v3", load_team_family("ranked", CORE_3V3_MODES), "win_label",
         TEAM_CATEGORICAL, TEAM_NUMERIC, {"type": "ranked", "team_size": 3, "modes": CORE_3V3_MODES}),
        ("solo_showdown_top4", load_showdown_family("soloShowdown", 4), "success_label",
         SHOWDOWN_CATEGORICAL, SHOWDOWN_NUMERIC, {"mode": "soloShowdown", "success_rank_max": 4}),
        ("duo_showdown_top2", load_showdown_family("duoShowdown", 2), "success_label",
         SHOWDOWN_CATEGORICAL, SHOWDOWN_NUMERIC, {"mode": "duoShowdown", "success_rank_max": 2}),
    ]
    reports = [train_family(*args) for args in families]
    summary = {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "families": reports}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    for report in reports:
        m = report["metrics"]
        print(report["family"], report["rows"], f"auc={m['roc_auc']:.4f}", f"brier={m['brier']:.4f}")


if __name__ == "__main__":
    main()
