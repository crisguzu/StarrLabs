#!/usr/bin/env python3
"""
Compute victory baselines + player rollups locally.

Supabase-hosted Postgres can cancel large GROUPING SETS rollups before they finish.
This script intentionally keeps Supabase work simple:
  1. stream only the needed 30-day feature rows with an indexed WHERE clause
  2. aggregate league/player rollups in Python on the self-hosted runner
  3. upsert compact result rows back to Supabase
"""
import math
import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import psycopg2
from database import connect as database_connect
from dotenv import load_dotenv
from psycopg2.extras import execute_values

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# =========================
# Env / config
# =========================
WINDOW_DAYS = int(os.getenv("VICTORY_WINDOW_DAYS", "30"))
SKILL_DIFF = float(os.getenv("VICTORY_SKILL_DIFF", "0.10"))

# Configurable “predicted win” threshold
PRED_THRESHOLD = float(os.getenv("VICTORY_PRED_THRESHOLD", "0.60"))

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")

FETCH_BATCH_SIZE = int(os.getenv("VICTORY_FETCH_BATCH_SIZE", "5000"))
UPSERT_BATCH_SIZE = int(os.getenv("VICTORY_UPSERT_BATCH_SIZE", "1000"))

# Difficulty buckets are probability cutoffs (must be floats)
DIFF_EASY = float(os.getenv("DIFF_EASY", "0.60"))
DIFF_NORMAL = float(os.getenv("DIFF_NORMAL", "0.40"))
DIFF_HARD = float(os.getenv("DIFF_HARD", "0.25"))

# Difficulty weights (must be floats)
W_EASY = float(os.getenv("W_EASY", "0.2"))
W_NORMAL = float(os.getenv("W_NORMAL", "1.0"))
W_HARD = float(os.getenv("W_HARD", "2.0"))
W_VERY_HARD = float(os.getenv("W_VERY_HARD", "5.0"))

# Victory Score weights
VS_BASE = float(os.getenv("VS_BASE", "50"))
VS_WR = float(os.getenv("VS_WR", "20"))
VS_OVERP = float(os.getenv("VS_OVERP", "30"))
VS_DIFF = float(os.getenv("VS_DIFF", "10"))

# Victory Score V2: Bayesian log-odds lift over published match probabilities.
VS_VERSION = os.getenv("VICTORY_SCORE_VERSION", "v2-bayes-lift-20260718")
VS_PRIOR_SD = float(os.getenv("VICTORY_V2_PRIOR_SD", "0.50"))
VS_POINTS_PER_LIFT_PP = float(os.getenv("VICTORY_V2_POINTS_PER_LIFT_PP", "2.5"))

# =========================
# Sentinel values for rollups
# (prevents NULLs violating NOT NULL constraints)
# =========================
ALL_DIM = os.getenv("VICTORY_ALL_DIM", "__ALL__")
UNKNOWN_DIM = os.getenv("VICTORY_UNKNOWN_DIM", "__UNKNOWN__")

# The app currently joins victory scores on the overall player row only:
# mode=__ALL__, type=__ALL__, brawler_name=__ALL__.
# Keeping only that published dimension avoids generating/upserting hundreds of
# thousands of unused per-mode/per-type/per-brawler rows in the daily pipeline.
LEAGUE_DIM_ROLLUPS = (
    (False, False, False),
)

PLAYER_DIM_ROLLUPS = LEAGUE_DIM_ROLLUPS

FEATURE_ROWS_SQL = """
SELECT
  player_tag,
  mode,
  type,
  COALESCE(brawler_name, %s) AS brawler_name,
  win_label,
  pred_win_prob
FROM public.battle_features_3v3
WHERE battletime >= now() - (%s * interval '1 day')
  AND pred_win_prob IS NOT NULL
  AND win_label IN (0, 1);
"""

UPSERT_LEAGUE_SQL = """
INSERT INTO public.league_victory_baselines (
  window_days, mode, type, brawler_name,
  n_battles, league_win_rate, league_avg_difficulty,
  predicted_wins, surprise_wins, chokes,
  updated_at
)
VALUES %s
ON CONFLICT (window_days, mode, type, brawler_name)
DO UPDATE SET
  n_battles = EXCLUDED.n_battles,
  league_win_rate = EXCLUDED.league_win_rate,
  league_avg_difficulty = EXCLUDED.league_avg_difficulty,
  predicted_wins = EXCLUDED.predicted_wins,
  surprise_wins = EXCLUDED.surprise_wins,
  chokes = EXCLUDED.chokes,
  updated_at = EXCLUDED.updated_at;
"""

UPSERT_PLAYER_SQL = """
INSERT INTO public.player_victory_rollups (
  player_tag, window_days, mode, type, brawler_name,
  n_battles, wins, losses,
  predicted_wins, surprise_wins, chokes,
  xw, win_rate, overperf,
  avg_difficulty, league_win_rate, league_avg_difficulty,
  league_predicted_wins, league_surprise_wins, league_chokes,
  pred_threshold,
  skill_diff,
  win_rate_score, overperf_score, difficulty_score, victory_score,
  victory_score_version, victory_score_v1, adjusted_lift,
  evidence_confidence, posterior_sd, rating_prior_sd,
  updated_at
)
VALUES %s
ON CONFLICT (player_tag, window_days, mode, type, brawler_name)
DO UPDATE SET
  n_battles = EXCLUDED.n_battles,
  wins = EXCLUDED.wins,
  losses = EXCLUDED.losses,
  predicted_wins = EXCLUDED.predicted_wins,
  surprise_wins = EXCLUDED.surprise_wins,
  chokes = EXCLUDED.chokes,
  xw = EXCLUDED.xw,
  win_rate = EXCLUDED.win_rate,
  overperf = EXCLUDED.overperf,
  avg_difficulty = EXCLUDED.avg_difficulty,
  league_win_rate = EXCLUDED.league_win_rate,
  league_avg_difficulty = EXCLUDED.league_avg_difficulty,
  league_predicted_wins = EXCLUDED.league_predicted_wins,
  league_surprise_wins  = EXCLUDED.league_surprise_wins,
  league_chokes         = EXCLUDED.league_chokes,
  pred_threshold = EXCLUDED.pred_threshold,
  skill_diff = EXCLUDED.skill_diff,
  win_rate_score = EXCLUDED.win_rate_score,
  overperf_score = EXCLUDED.overperf_score,
  difficulty_score = EXCLUDED.difficulty_score,
  victory_score = EXCLUDED.victory_score,
  victory_score_version = EXCLUDED.victory_score_version,
  victory_score_v1 = EXCLUDED.victory_score_v1,
  adjusted_lift = EXCLUDED.adjusted_lift,
  evidence_confidence = EXCLUDED.evidence_confidence,
  posterior_sd = EXCLUDED.posterior_sd,
  rating_prior_sd = EXCLUDED.rating_prior_sd,
  updated_at = EXCLUDED.updated_at;
"""


LEAGUE_VALUES_TEMPLATE = """(
  %(window_days)s, %(mode)s, %(type)s, %(brawler_name)s,
  %(n_battles)s, %(league_win_rate)s, %(league_avg_difficulty)s,
  %(predicted_wins)s, %(surprise_wins)s, %(chokes)s,
  now()
)"""

PLAYER_VALUES_TEMPLATE = """(
  %(player_tag)s, %(window_days)s, %(mode)s, %(type)s, %(brawler_name)s,
  %(n_battles)s, %(wins)s, %(losses)s,
  %(predicted_wins)s, %(surprise_wins)s, %(chokes)s,
  %(xw)s, %(win_rate)s, %(overperf)s,
  %(avg_difficulty)s, %(league_win_rate)s, %(league_avg_difficulty)s,
  %(league_predicted_wins)s, %(league_surprise_wins)s, %(league_chokes)s,
  %(pred_threshold)s,
  %(skill_diff)s,
  %(win_rate_score)s, %(overperf_score)s, %(difficulty_score)s, %(victory_score)s,
  %(victory_score_version)s, %(victory_score_v1)s, %(adjusted_lift)s,
  %(evidence_confidence)s, %(posterior_sd)s, %(rating_prior_sd)s,
  now()
)"""

UPSERT_HISTORY_SQL = """
INSERT INTO public.player_victory_score_history (
  snapshot_date, captured_at, player_tag, window_days, mode, type, brawler_name,
  victory_score, victory_score_version, n_battles, wins, expected_wins,
  adjusted_lift, evidence_confidence
)
VALUES %s
ON CONFLICT (snapshot_date, player_tag, window_days, mode, type, brawler_name)
DO UPDATE SET
  captured_at = EXCLUDED.captured_at,
  victory_score = EXCLUDED.victory_score,
  victory_score_version = EXCLUDED.victory_score_version,
  n_battles = EXCLUDED.n_battles,
  wins = EXCLUDED.wins,
  expected_wins = EXCLUDED.expected_wins,
  adjusted_lift = EXCLUDED.adjusted_lift,
  evidence_confidence = EXCLUDED.evidence_confidence;
"""

HISTORY_VALUES_TEMPLATE = """(
  current_date, now(), %(player_tag)s, %(window_days)s, %(mode)s, %(type)s, %(brawler_name)s,
  %(victory_score)s, %(victory_score_version)s, %(n_battles)s, %(wins)s, %(xw)s,
  %(adjusted_lift)s, %(evidence_confidence)s
)"""


@dataclass
class RollupStats:
    n_battles: int = 0
    wins: int = 0
    predicted_wins: int = 0
    surprise_wins: int = 0
    chokes: int = 0
    xw: float = 0.0
    difficulty_sum: float = 0.0
    games: list[tuple[int, float]] = field(default_factory=list)

    def add(self, win_label: int, pred_win_prob: float, difficulty_weight: float) -> None:
        self.n_battles += 1
        self.wins += int(win_label)
        self.predicted_wins += int(pred_win_prob >= PRED_THRESHOLD)
        self.surprise_wins += int(win_label == 1 and pred_win_prob < PRED_THRESHOLD)
        self.chokes += int(win_label == 0 and pred_win_prob >= PRED_THRESHOLD)
        self.xw += float(pred_win_prob)
        self.difficulty_sum += float(difficulty_weight)
        self.games.append((int(win_label), float(pred_win_prob)))

    @property
    def losses(self) -> int:
        return self.n_battles - self.wins

    @property
    def win_rate(self) -> Optional[float]:
        return self.wins / self.n_battles if self.n_battles else None

    @property
    def overperf(self) -> Optional[float]:
        return self.wins - self.xw if self.n_battles else None

    @property
    def avg_difficulty(self) -> Optional[float]:
        return self.difficulty_sum / self.n_battles if self.n_battles else None


def get_conn():
    return database_connect()


def difficulty_weight(pred_win_prob: float) -> float:
    if pred_win_prob >= DIFF_EASY:
        return W_EASY
    if pred_win_prob >= DIFF_NORMAL:
        return W_NORMAL
    if pred_win_prob >= DIFF_HARD:
        return W_HARD
    return W_VERY_HARD


def rolled_dim_key(mode, btype, brawler_name, keep_mode: bool, keep_type: bool, keep_brawler: bool):
    return (
        mode if keep_mode else None,
        btype if keep_type else None,
        brawler_name if keep_brawler else None,
    )


def stored_dim(value):
    return value if value is not None else ALL_DIM


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def logistic(value: float) -> float:
    if value >= 0:
        exp_neg = math.exp(-value)
        return 1.0 / (1.0 + exp_neg)
    exp_pos = math.exp(value)
    return exp_pos / (1.0 + exp_pos)


def logit(probability: float) -> float:
    probability = clamp(float(probability), 1e-6, 1.0 - 1e-6)
    return math.log(probability / (1.0 - probability))


def bayesian_effect_score(
    games: list[tuple[int, float]],
    prior_sd: float = VS_PRIOR_SD,
    points_per_lift_pp: float = VS_POINTS_PER_LIFT_PP,
) -> dict:
    """Return bounded V2 score and evidence from a regularized log-odds lift."""
    if not games:
        return {"victory_score": None, "adjusted_lift": None,
                "evidence_confidence": 0.0, "posterior_sd": prior_sd}
    if prior_sd <= 0 or points_per_lift_pp <= 0:
        raise ValueError("V2 prior and scale must be positive")

    theta = 0.0
    for _ in range(30):
        adjusted = [logistic(logit(pred) + theta) for _, pred in games]
        gradient = sum(result - prob for (result, _), prob in zip(games, adjusted)) - theta / (prior_sd ** 2)
        hessian = -sum(prob * (1.0 - prob) for prob in adjusted) - 1.0 / (prior_sd ** 2)
        step = gradient / hessian
        theta -= step
        if abs(step) < 1e-10:
            break

    adjusted = [logistic(logit(pred) + theta) for _, pred in games]
    hessian = -sum(prob * (1.0 - prob) for prob in adjusted) - 1.0 / (prior_sd ** 2)
    posterior_sd = math.sqrt(-1.0 / hessian)
    adjusted_lift = sum(prob - pred for prob, (_, pred) in zip(adjusted, games)) / len(games)
    evidence_confidence = clamp(1.0 - posterior_sd / prior_sd, 0.0, 1.0)
    victory_score = clamp(50.0 + points_per_lift_pp * adjusted_lift * 100.0, 0.0, 100.0)
    return {
        "victory_score": victory_score,
        "adjusted_lift": adjusted_lift,
        "evidence_confidence": evidence_confidence,
        "posterior_sd": posterior_sd,
    }


def build_rollups():
    league = defaultdict(RollupStats)
    players = defaultdict(RollupStats)
    rows_read = 0

    conn = get_conn()
    try:
        # Named cursor streams rows instead of materializing the full result set in memory.
        with conn.cursor(name="victory_feature_rows") as cur:
            cur.itersize = FETCH_BATCH_SIZE
            cur.execute(FEATURE_ROWS_SQL, (UNKNOWN_DIM, WINDOW_DAYS))

            while True:
                batch = cur.fetchmany(FETCH_BATCH_SIZE)
                if not batch:
                    break

                for player_tag, mode, btype, brawler_name, win_label, pred_win_prob in batch:
                    pred = float(pred_win_prob)
                    diff = difficulty_weight(pred)
                    win = int(win_label)
                    rows_read += 1

                    if rows_read % 50000 == 0:
                        print(f"  processed feature rows={rows_read} ...", flush=True)

                    for keep_mode, keep_type, keep_brawler in LEAGUE_DIM_ROLLUPS:
                        key = rolled_dim_key(mode, btype, brawler_name, keep_mode, keep_type, keep_brawler)
                        league[key].add(win, pred, diff)

                    for keep_mode, keep_type, keep_brawler in PLAYER_DIM_ROLLUPS:
                        dim_key = rolled_dim_key(mode, btype, brawler_name, keep_mode, keep_type, keep_brawler)
                        players[(player_tag, *dim_key)].add(win, pred, diff)
    finally:
        conn.close()

    return rows_read, league, players


def league_rows(league) -> list[dict]:
    rows = []
    for (mode, btype, brawler_name), stats in league.items():
        rows.append(
            {
                "window_days": WINDOW_DAYS,
                "mode": stored_dim(mode),
                "type": stored_dim(btype),
                "brawler_name": stored_dim(brawler_name),
                "n_battles": stats.n_battles,
                "league_win_rate": stats.win_rate,
                "league_avg_difficulty": stats.avg_difficulty,
                "predicted_wins": stats.predicted_wins,
                "surprise_wins": stats.surprise_wins,
                "chokes": stats.chokes,
            }
        )
    return rows


def player_rows(players, league) -> list[dict]:
    rows = []
    for (player_tag, mode, btype, brawler_name), stats in players.items():
        league_stats = league.get((mode, btype, brawler_name))
        if not league_stats:
            # Should not happen because league and player rollups are built from the same stream.
            continue

        win_rate = stats.win_rate
        overperf = stats.overperf
        avg_difficulty = stats.avg_difficulty
        league_win_rate = league_stats.win_rate
        league_avg_difficulty = league_stats.avg_difficulty

        win_rate_score = None
        if win_rate is not None and league_win_rate is not None and SKILL_DIFF:
            win_rate_score = (win_rate - league_win_rate) / SKILL_DIFF

        overperf_score = None
        if overperf is not None and stats.n_battles:
            overperf_score = clamp(overperf / math.sqrt(stats.n_battles), -2.5, 2.5)

        difficulty_score = None
        if avg_difficulty is not None and league_avg_difficulty:
            difficulty_score = clamp(avg_difficulty / league_avg_difficulty, 0.5, 1.5)

        victory_score_v1 = None
        if win_rate_score is not None and overperf_score is not None and difficulty_score is not None:
            victory_score_v1 = (
                VS_BASE
                + VS_WR * win_rate_score
                + VS_OVERP * overperf_score
                + VS_DIFF * difficulty_score
            )
        v2 = bayesian_effect_score(stats.games)

        rows.append(
            {
                "player_tag": player_tag,
                "window_days": WINDOW_DAYS,
                "mode": stored_dim(mode),
                "type": stored_dim(btype),
                "brawler_name": stored_dim(brawler_name),
                "n_battles": stats.n_battles,
                "wins": stats.wins,
                "losses": stats.losses,
                "predicted_wins": stats.predicted_wins,
                "surprise_wins": stats.surprise_wins,
                "chokes": stats.chokes,
                "xw": stats.xw,
                "win_rate": win_rate,
                "overperf": overperf,
                "avg_difficulty": avg_difficulty,
                "league_win_rate": league_win_rate,
                "league_avg_difficulty": league_avg_difficulty,
                "league_predicted_wins": league_stats.predicted_wins,
                "league_surprise_wins": league_stats.surprise_wins,
                "league_chokes": league_stats.chokes,
                "pred_threshold": PRED_THRESHOLD,
                "skill_diff": SKILL_DIFF,
                "win_rate_score": win_rate_score,
                "overperf_score": overperf_score,
                "difficulty_score": difficulty_score,
                "victory_score": v2["victory_score"],
                "victory_score_version": VS_VERSION,
                "victory_score_v1": victory_score_v1,
                "adjusted_lift": v2["adjusted_lift"],
                "evidence_confidence": v2["evidence_confidence"],
                "posterior_sd": v2["posterior_sd"],
                "rating_prior_sd": VS_PRIOR_SD,
            }
        )
    return rows


def batched(items: list[dict], size: int) -> Iterable[list[dict]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def upsert_results(league_out: list[dict], player_out: list[dict]) -> None:
    conn = get_conn()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            for batch in batched(league_out, UPSERT_BATCH_SIZE):
                execute_values(
                    cur,
                    UPSERT_LEAGUE_SQL,
                    batch,
                    template=LEAGUE_VALUES_TEMPLATE,
                    page_size=UPSERT_BATCH_SIZE,
                )
            print(f"  upserted league rows={len(league_out)}", flush=True)
            upserted_players = 0
            for batch in batched(player_out, UPSERT_BATCH_SIZE):
                execute_values(
                    cur,
                    UPSERT_PLAYER_SQL,
                    batch,
                    template=PLAYER_VALUES_TEMPLATE,
                    page_size=UPSERT_BATCH_SIZE,
                )
                upserted_players += len(batch)
                if upserted_players % (UPSERT_BATCH_SIZE * 10) == 0 or upserted_players == len(player_out):
                    print(f"  upserted player rows={upserted_players}/{len(player_out)}", flush=True)
            for batch in batched(player_out, UPSERT_BATCH_SIZE):
                execute_values(
                    cur,
                    UPSERT_HISTORY_SQL,
                    batch,
                    template=HISTORY_VALUES_TEMPLATE,
                    page_size=UPSERT_BATCH_SIZE,
                )
            print(f"  saved daily score snapshots={len(player_out)}", flush=True)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    missing = [k for k in ["DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME"] if not os.getenv(k)]
    if missing:
        raise SystemExit(f"Missing env vars: {', '.join(missing)}")

    print(
        "Computing victory baselines + rollups locally "
        f"(window_days={WINDOW_DAYS}, pred_threshold={PRED_THRESHOLD}, fetch_batch={FETCH_BATCH_SIZE}) ..."
    )
    rows_read, league, players = build_rollups()
    print(f"Fetched feature rows={rows_read}; league_rollups={len(league)}; player_rollups={len(players)}")

    league_out = league_rows(league)
    player_out = player_rows(players, league)
    print(f"Upserting compact results: league_rows={len(league_out)}, player_rows={len(player_out)}")

    upsert_results(league_out, player_out)
    print("✅ Done. Local baselines + rollups computed and upserted.")


if __name__ == "__main__":
    main()
