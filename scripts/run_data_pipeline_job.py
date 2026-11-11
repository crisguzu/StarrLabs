#!/usr/bin/env python3
"""Run one explicitly selected, serialized self-hosted data-pipeline job.

This source distribution installs no scheduler or service and ships no GitHub
workflow. Historical deployment schedules are not part of the public package.
Operators must configure their own authorized data, credentials and bounded job
execution. This CLI can mutate the configured database; it is not a read-only check.
"""

import argparse
import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = Path(os.getenv("STARRLABS_PIPELINE_LOCK", "/tmp/starrlabs-data-pipeline.lock"))
LOCK_WAIT_SECONDS = float(os.getenv("STARRLABS_PIPELINE_LOCK_WAIT_SECONDS", "600"))
LOCK_RETRY_SECONDS = float(os.getenv("STARRLABS_PIPELINE_LOCK_RETRY_SECONDS", "2"))
TEMPORARY_FAILURE = 75

JOBS = {
    "battle": [
        ["services/club_collector.py"],
        ["services/collector.py"],
        ["services/opponent_profile_collector.py"],
        ["services/scout_profile_retry.py"],
    ],
    "profiles": [["services/profile_updater.py"]],
    "brawler-activity": [["services/player_brawler_daily_activity.py"]],
    "brawler-daily": [["populate/player_brawler_daily.py"]],
    "daily-activity": [["services/daily_activity.py"]],
    "reference": [
        ["services/reference_collector.py"],
        ["services/brawlapi_reference_collector.py"],
        ["services/game_data_deriver.py"],
    ],
    "retention": [["scripts/apply_retention.py", "--apply"]],
    "health": [
        ["models/refresh_loadout_features_3v3.py"],
        ["models/score_loadout_shadow.py"],
        ["scripts/audit_data_pipelines.py"],
    ],
    "weekly-populate": [
        ["populate/brawlers.py"],
        ["populate/game-modes.py"],
        ["populate/maps.py"],
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job", choices=sorted(JOBS))
    args = parser.parse_args()

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("DB_TARGET", "self-hosted-postgres")
    env.setdefault("DB_EXPECTED_NAME", "starrlabs")
    env.setdefault("DB_ALLOWED_HOSTS", "127.0.0.1,localhost")
    env.setdefault("REJECT_SUPABASE_HOSTS", "1")

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w") as lock:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        waited = False
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if waited:
                    print(f"PIPELINE_LOCK_ACQUIRED|{args.job}|continued after contention", flush=True)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    print(
                        f"PIPELINE_RETRY_REQUIRED|{args.job}|lock remained busy for "
                        f"{LOCK_WAIT_SECONDS:g}s",
                        flush=True,
                    )
                    return TEMPORARY_FAILURE
                if not waited:
                    print(
                        f"PIPELINE_WAITING|{args.job}|another selected data job is active",
                        flush=True,
                    )
                    waited = True
                time.sleep(LOCK_RETRY_SECONDS)

        for command in JOBS[args.job]:
            print(f"PIPELINE_STEP|{args.job}|{' '.join(command)}", flush=True)
            subprocess.run([sys.executable, *command], cwd=ROOT, env=env, check=True)

    print(f"PIPELINE_SUCCEEDED|{args.job}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
