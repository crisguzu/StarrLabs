#!/usr/bin/env python3
"""Audited runner for scheduled and MISSY-triggered prediction maintenance."""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from database import connect


ROOT = Path(__file__).resolve().parents[1]


def update(run_id, **values):
    assignments = []
    params = []
    for key, value in values.items():
        assignments.append(f"{key} = %s")
        params.append(json.dumps(value) if key == "details" else value)
    params.append(str(run_id))
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE missy_maintenance_runs SET {', '.join(assignments)} WHERE id = %s",
            params,
        )


def command(label, args, env):
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, *args], cwd=ROOT, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=1800,
    )
    output = (result.stdout or "").strip()
    if result.returncode:
        raise RuntimeError(f"{label} failed ({result.returncode}): {output[-1200:]}")
    return {"step": label, "seconds": round(time.monotonic() - started, 2), "summary": output[-500:]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", choices=("predictions", "victory", "all"), default="all")
    parser.add_argument("--trigger", choices=("manual", "scheduled"), default="scheduled")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    run_id = uuid.UUID(args.run_id) if args.run_id else uuid.uuid4()
    requested_by = os.getenv("BSA_MAINTENANCE_REQUESTED_BY") or None

    with connect() as conn, conn.cursor() as cur:
        # systemd kills this unit after 30 minutes. If the process is terminated
        # before its exception handler runs, a stale row must not block every
        # future hourly refresh indefinitely.
        cur.execute(
            """
            UPDATE missy_maintenance_runs
            SET status = 'failed',
                finished_at = now(),
                error_message = COALESCE(error_message, 'Stale maintenance run recovered automatically')
            WHERE status IN ('queued', 'running')
              AND COALESCE(started_at, requested_at) < now() - interval '40 minutes'
            """
        )
        cur.execute(
            "SELECT id FROM missy_maintenance_runs WHERE status IN ('queued','running') AND id <> %s LIMIT 1",
            (str(run_id),),
        )
        active = cur.fetchone()
        if not args.run_id:
            cur.execute(
                """INSERT INTO missy_maintenance_runs(
                     id,job_type,trigger_type,requested_by,status,finished_at,error_message
                   ) VALUES (%s,%s,%s,%s,%s,
                     CASE WHEN %s THEN now() ELSE NULL END,
                     CASE WHEN %s THEN 'Another maintenance run is active' ELSE NULL END
                   )""",
                (str(run_id), args.job, args.trigger, requested_by,
                 "skipped" if active else "queued", bool(active), bool(active)),
            )
        if active:
            if args.run_id:
                cur.execute(
                    "UPDATE missy_maintenance_runs SET status='skipped',finished_at=now(),error_message='Another maintenance run is active' WHERE id=%s",
                    (str(run_id),),
                )
            print(f"MAINTENANCE_SKIPPED|{run_id}")
            return

    started = time.monotonic()
    update(run_id, status="running")
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE missy_maintenance_runs SET started_at=now() WHERE id=%s", (str(run_id),))

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("MODEL_VERSION", "ladder-catboost-20260716")
    steps = []
    try:
        if args.job in ("predictions", "all"):
            feature_env = env.copy(); feature_env["DAYS_BACK"] = "1"
            feature_env["CHUNK_HOURS"] = "6"
            steps.append(command("refresh_features", ["models/refresh_battle_features_3v3.py"], feature_env))
            steps.append(command("predict_ladder", ["models/predict_victory_model.py"], env))
        if args.job in ("victory", "all"):
            steps.append(command("victory_rollups", ["services/victory_score_calculation.py"], env))
        elapsed = round(time.monotonic() - started, 2)
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE missy_maintenance_runs SET status='succeeded',finished_at=now(),
                   duration_seconds=%s,details=%s::jsonb WHERE id=%s""",
                (elapsed, json.dumps({"steps": steps}), str(run_id)),
            )
        print(f"MAINTENANCE_SUCCEEDED|{run_id}|{elapsed}")
    except Exception as exc:
        elapsed = round(time.monotonic() - started, 2)
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE missy_maintenance_runs SET status='failed',finished_at=now(),
                   duration_seconds=%s,error_message=%s WHERE id=%s""",
                (elapsed, str(exc)[-1600:], str(run_id)),
            )
        raise


if __name__ == "__main__":
    main()
