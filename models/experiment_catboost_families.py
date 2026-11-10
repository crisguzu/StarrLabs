#!/usr/bin/env python3
"""Offline CatBoost challengers for the approved BSA model families."""

import json
from pathlib import Path

import joblib
import numpy as np
from catboost import CatBoostClassifier
from sklearn.linear_model import LogisticRegression

from models.train_model_families import (
    CORE_3V3_MODES, RANKED_3V3_MODES, SHOWDOWN_CATEGORICAL, SHOWDOWN_NUMERIC,
    TEAM_CATEGORICAL, TEAM_NUMERIC, load_showdown_family, load_team_family, metrics,
)


OUT = Path(__file__).resolve().parent / "artifacts" / "families"


def prepared(frame, categorical, numeric):
    x = frame[categorical + numeric].copy()
    for col in categorical:
        x[col] = x[col].fillna("UNKNOWN").astype(str)
    for col in numeric:
        # CatBoost handles numeric NaN natively. Do not compute a separate
        # median inside train/calibration/test or inference frames.
        x[col] = x[col].replace([np.inf, -np.inf], np.nan)
    return x


def platt(cal_y, cal_p, test_p):
    eps = 1e-6
    cal_logit = np.log(np.clip(cal_p, eps, 1-eps) / np.clip(1-cal_p, eps, 1-eps)).reshape(-1, 1)
    test_logit = np.log(np.clip(test_p, eps, 1-eps) / np.clip(1-test_p, eps, 1-eps)).reshape(-1, 1)
    calibrator = LogisticRegression(C=1e6, solver="lbfgs").fit(cal_logit, cal_y)
    return calibrator, calibrator.predict_proba(test_logit)[:, 1]


def run(name, df, target, categorical, numeric):
    df = df.sort_values("battletime", kind="stable").reset_index(drop=True)
    n = len(df); a = int(n*.70); b = int(n*.85)
    train, cal, test = df.iloc[:a], df.iloc[a:b], df.iloc[b:]
    x_train = prepared(train, categorical, numeric)
    x_cal = prepared(cal, categorical, numeric)
    x_test = prepared(test, categorical, numeric)
    model = CatBoostClassifier(
        iterations=500, depth=7, learning_rate=.04, loss_function="Logloss",
        eval_metric="AUC", l2_leaf_reg=6, random_seed=42, thread_count=2,
        verbose=False, allow_writing_files=False,
    )
    model.fit(x_train, train[target].astype(int), cat_features=categorical,
              eval_set=(x_cal, cal[target].astype(int)), early_stopping_rounds=60, verbose=False)
    raw_cal = model.predict_proba(x_cal)[:, 1]
    raw_test = model.predict_proba(x_test)[:, 1]
    calibrator, p = platt(cal[target].astype(int).to_numpy(), raw_cal, raw_test)
    report = {"family": name, "model": "catboost_platt", "rows": n,
              "best_iteration": model.get_best_iteration(),
              "metrics": metrics(test[target].astype(int).to_numpy(), p)}
    model.save_model(str(OUT / f"{name}.catboost.cbm"))
    joblib.dump(calibrator, OUT / f"{name}.catboost.platt.joblib")
    (OUT / f"{name}.catboost.json").write_text(json.dumps(report, indent=2))
    print(name, report)
    return report


def main():
    reports = [
        run("ranked_3v3", load_team_family("soloRanked", RANKED_3V3_MODES), "win_label", TEAM_CATEGORICAL, TEAM_NUMERIC),
        run("ladder_3v3", load_team_family("ranked", CORE_3V3_MODES), "win_label", TEAM_CATEGORICAL, TEAM_NUMERIC),
        run("solo_showdown_top4", load_showdown_family("soloShowdown", 4), "success_label", SHOWDOWN_CATEGORICAL, SHOWDOWN_NUMERIC),
        run("duo_showdown_top2", load_showdown_family("duoShowdown", 2), "success_label", SHOWDOWN_CATEGORICAL, SHOWDOWN_NUMERIC),
    ]
    (OUT / "catboost_summary.json").write_text(json.dumps({"families": reports}, indent=2))


if __name__ == "__main__":
    main()
