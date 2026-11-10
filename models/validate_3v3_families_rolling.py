#!/usr/bin/env python3
"""Rolling-origin validation for exact-3v3 Ladder and competitive Ranked models."""

import json
from pathlib import Path

import numpy as np
from catboost import CatBoostClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.frozen import FrozenEstimator

from models.experiment_catboost_families import platt, prepared
from models.train_model_families import (
    CORE_3V3_MODES, RANKED_3V3_MODES, TEAM_CATEGORICAL, TEAM_NUMERIC,
    estimator, load_team_family, metrics,
)


OUT = Path(__file__).resolve().parent / "artifacts" / "families" / "rolling_3v3.json"


def evaluate_fold(df, start_frac, end_frac):
    n = len(df); test_start = int(n * start_frac); test_end = int(n * end_frac)
    calibration_start = int(test_start * .82)
    train, cal, test = df.iloc[:calibration_start], df.iloc[calibration_start:test_start], df.iloc[test_start:test_end]
    features = TEAM_CATEGORICAL + TEAM_NUMERIC
    y_train = train.win_label.astype(int); y_cal = cal.win_label.astype(int); y_test = test.win_label.astype(int)

    logistic = estimator(TEAM_CATEGORICAL, TEAM_NUMERIC).fit(train[features], y_train)
    logistic_cal = CalibratedClassifierCV(FrozenEstimator(logistic), method="sigmoid").fit(cal[features], y_cal)
    logistic_p = logistic_cal.predict_proba(test[features])[:, 1]

    x_train = prepared(train, TEAM_CATEGORICAL, TEAM_NUMERIC)
    x_cal = prepared(cal, TEAM_CATEGORICAL, TEAM_NUMERIC)
    x_test = prepared(test, TEAM_CATEGORICAL, TEAM_NUMERIC)
    cat = CatBoostClassifier(
        iterations=500, depth=7, learning_rate=.04, loss_function="Logloss", eval_metric="AUC",
        l2_leaf_reg=6, random_seed=42, thread_count=2, verbose=False, allow_writing_files=False,
    )
    cat.fit(x_train, y_train, cat_features=TEAM_CATEGORICAL, eval_set=(x_cal, y_cal),
            early_stopping_rounds=60, verbose=False)
    cat_platt, cat_p = platt(y_cal.to_numpy(), cat.predict_proba(x_cal)[:, 1], cat.predict_proba(x_test)[:, 1])
    del cat_platt

    return {
        "ranges": {
            "train": [train.battletime.min().isoformat(), train.battletime.max().isoformat()],
            "calibration": [cal.battletime.min().isoformat(), cal.battletime.max().isoformat()],
            "test": [test.battletime.min().isoformat(), test.battletime.max().isoformat()],
        },
        "rows": {"train": len(train), "calibration": len(cal), "test": len(test)},
        "logistic": metrics(y_test.to_numpy(), logistic_p),
        "catboost": metrics(y_test.to_numpy(), cat_p),
        "catboost_best_iteration": cat.get_best_iteration(),
    }


def validate(name, df):
    df = df.sort_values("battletime", kind="stable").reset_index(drop=True)
    folds = [evaluate_fold(df, a, b) for a, b in ((.55, .70), (.70, .85), (.85, 1.0))]
    summary = {}
    for model in ("logistic", "catboost"):
        summary[model] = {
            key: float(np.mean([fold[model][key] for fold in folds]))
            for key in ("roc_auc", "brier", "log_loss")
        }
        summary[model]["auc_min"] = float(min(fold[model]["roc_auc"] for fold in folds))
        summary[model]["auc_max"] = float(max(fold[model]["roc_auc"] for fold in folds))
    return {"family": name, "rows": len(df), "folds": folds, "summary": summary}


def main():
    reports = [
        validate("ranked_3v3", load_team_family("soloRanked", RANKED_3V3_MODES)),
        validate("ladder_3v3", load_team_family("ranked", CORE_3V3_MODES)),
    ]
    OUT.write_text(json.dumps({"families": reports}, indent=2))
    for report in reports:
        print(report["family"], json.dumps(report["summary"], sort_keys=True))


if __name__ == "__main__":
    main()
