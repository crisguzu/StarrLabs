#!/usr/bin/env python3
import os
import datetime as dt

import psycopg2
from database import connect as database_connect
from pathlib import Path
from dotenv import load_dotenv

# Load .env (same pattern as services/daily_activity.py)
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")
STATEMENT_TIMEOUT_MS = int(os.getenv("STATEMENT_TIMEOUT_MS", "900000"))

# How many days back to recompute (override: DAYS_BACK=30 python models/refresh_battle_features_3v3.py)
DAYS_BACK = int(os.getenv("DAYS_BACK", "3"))
CHUNK_HOURS = int(os.getenv("CHUNK_HOURS", "240"))


def get_conn():
    if not all([DB_HOST, DB_USER, DB_PASSWORD, DB_NAME]):
        raise RuntimeError("Some DB_* variables are missing in environment")
    return database_connect()


UPSERT_SQL = r"""
with base as (
  select
    b.*,

    -- ✅ use a unique derived name to avoid colliding with any existing battles.battletime_ts column
    b.battletime_ts as feature_battletime_ts,

    coalesce(b.map_id::text, b.map) as map_key,

    case
      when b.result = 'victory' then 1
      when b.result = 'defeat'  then 0
      else null
    end as win_label,

    case
      when b.result = 'victory' then 'defeat'
      when b.result = 'defeat'  then 'victory'
      else null
    end as enemy_result
  from battles b
  where b.mode not in ('soloShowdown','duoShowdown')
    and b.result in ('victory','defeat')
    and b.battletime_ts >= %s
    and b.battletime_ts < %s
),

team_agg as (
  select
    base.feature_battletime_ts as battletime,
    base.mode,
    base.map_key,
    base.type,
    base.duration,
    base.result as team_result,

    count(*) as team_size,

    -- player_total_trophies
    avg(base.player_total_trophies) as team_avg_player_total_trophies,
    stddev_samp(base.player_total_trophies) as team_std_player_total_trophies,
    max(base.player_total_trophies) as team_max_player_total_trophies,

    -- player_level (avg + min/max/std)
    avg(base.player_level) as team_avg_player_level,
    min(base.player_level) as team_min_player_level,
    max(base.player_level) as team_max_player_level,
    stddev_samp(base.player_level) as team_std_player_level,

    -- brawler_power
    avg(base.brawler_power) as team_avg_brawler_power,
    min(base.brawler_power) as team_min_brawler_power,
    max(base.brawler_power) as team_max_brawler_power,

    -- ONLY brawler_total_trophies (avg + min/max/std)
    avg(base.brawler_total_trophies) as team_avg_brawler_total_trophies,
    min(base.brawler_total_trophies) as team_min_brawler_total_trophies,
    max(base.brawler_total_trophies) as team_max_brawler_total_trophies,
    stddev_samp(base.brawler_total_trophies) as team_std_brawler_total_trophies,

    -- familiarity / usage (avg + min/max/std for rank)
    avg(base.brawler_rank_on_account) as team_avg_brawler_rank_on_account,
    min(base.brawler_rank_on_account) as team_min_brawler_rank_on_account,
    max(base.brawler_rank_on_account) as team_max_brawler_rank_on_account,
    stddev_samp(base.brawler_rank_on_account) as team_std_brawler_rank_on_account,

    avg((base.is_highest_player_brawler)::int) as team_pct_highest_player_brawler,

    -- canonical (order-independent) team comp key
    string_agg(
      upper(trim(coalesce(base.brawler_name,'UNKNOWN'))),
      '|' order by upper(trim(coalesce(base.brawler_name,'UNKNOWN')))
    ) as team_comp_key

  from base
  group by 1,2,3,4,5,6
),

enemy_agg as (
  select
    base.feature_battletime_ts as battletime,
    base.mode,
    base.map_key,
    base.type,
    base.duration,
    base.result as enemy_team_result,

    count(*) as enemy_size,

    -- player_total_trophies
    avg(base.player_total_trophies) as enemy_avg_player_total_trophies,
    stddev_samp(base.player_total_trophies) as enemy_std_player_total_trophies,
    max(base.player_total_trophies) as enemy_max_player_total_trophies,

    -- player_level (avg + min/max/std)
    avg(base.player_level) as enemy_avg_player_level,
    min(base.player_level) as enemy_min_player_level,
    max(base.player_level) as enemy_max_player_level,
    stddev_samp(base.player_level) as enemy_std_player_level,

    -- brawler_power
    avg(base.brawler_power) as enemy_avg_brawler_power,
    min(base.brawler_power) as enemy_min_brawler_power,
    max(base.brawler_power) as enemy_max_brawler_power,

    -- ONLY brawler_total_trophies (avg + min/max/std)
    avg(base.brawler_total_trophies) as enemy_avg_brawler_total_trophies,
    min(base.brawler_total_trophies) as enemy_min_brawler_total_trophies,
    max(base.brawler_total_trophies) as enemy_max_brawler_total_trophies,
    stddev_samp(base.brawler_total_trophies) as enemy_std_brawler_total_trophies,

    -- familiarity / usage (avg + min/max/std for rank)
    avg(base.brawler_rank_on_account) as enemy_avg_brawler_rank_on_account,
    min(base.brawler_rank_on_account) as enemy_min_brawler_rank_on_account,
    max(base.brawler_rank_on_account) as enemy_max_brawler_rank_on_account,
    stddev_samp(base.brawler_rank_on_account) as enemy_std_brawler_rank_on_account,

    avg((base.is_highest_player_brawler)::int) as enemy_pct_highest_player_brawler,

    -- canonical (order-independent) enemy comp key
    string_agg(
      upper(trim(coalesce(base.brawler_name,'UNKNOWN'))),
      '|' order by upper(trim(coalesce(base.brawler_name,'UNKNOWN')))
    ) as enemy_comp_key

  from base
  group by 1,2,3,4,5,6
),

final as (
  select
    -- ✅ INSERT timestamptz
    b.feature_battletime_ts as battletime,

    b.mode,
    b.type,
    b.duration,
    b.map_id,
    b.map,
    b.map_key,

    b.player_tag,
    b.player_name,
    b.brawler_name,

    b.win_label,

    b.player_total_trophies,
    b.player_level,

    b.brawler_power,
    b.brawler_total_trophies,
    b.brawler_rank_on_account,

    b.is_starplayer,
    b.is_highest_player_brawler,

    b.rank,
    b.trophychange,

    -- team
    t.team_size,
    t.team_avg_player_total_trophies,
    t.team_std_player_total_trophies,
    t.team_max_player_total_trophies,

    t.team_avg_player_level,
    t.team_min_player_level,
    t.team_max_player_level,
    t.team_std_player_level,

    t.team_avg_brawler_power,
    t.team_min_brawler_power,
    t.team_max_brawler_power,

    t.team_avg_brawler_total_trophies,
    t.team_min_brawler_total_trophies,
    t.team_max_brawler_total_trophies,
    t.team_std_brawler_total_trophies,

    t.team_avg_brawler_rank_on_account,
    t.team_min_brawler_rank_on_account,
    t.team_max_brawler_rank_on_account,
    t.team_std_brawler_rank_on_account,

    t.team_pct_highest_player_brawler,
    t.team_comp_key,

    -- enemy
    e.enemy_size,
    e.enemy_avg_player_total_trophies,
    e.enemy_std_player_total_trophies,
    e.enemy_max_player_total_trophies,

    e.enemy_avg_player_level,
    e.enemy_min_player_level,
    e.enemy_max_player_level,
    e.enemy_std_player_level,

    e.enemy_avg_brawler_power,
    e.enemy_min_brawler_power,
    e.enemy_max_brawler_power,

    e.enemy_avg_brawler_total_trophies,
    e.enemy_min_brawler_total_trophies,
    e.enemy_max_brawler_total_trophies,
    e.enemy_std_brawler_total_trophies,

    e.enemy_avg_brawler_rank_on_account,
    e.enemy_min_brawler_rank_on_account,
    e.enemy_max_brawler_rank_on_account,
    e.enemy_std_brawler_rank_on_account,

    e.enemy_pct_highest_player_brawler,
    e.enemy_comp_key,

    -- matchup key
    (t.team_comp_key || ' vs ' || e.enemy_comp_key) as matchup_key,

    -- diffs
    (t.team_avg_player_total_trophies - e.enemy_avg_player_total_trophies) as diff_avg_player_total_trophies,
    (t.team_std_player_total_trophies - e.enemy_std_player_total_trophies) as diff_std_player_total_trophies,

    (t.team_avg_player_level - e.enemy_avg_player_level) as diff_avg_player_level,
    (t.team_min_player_level - e.enemy_min_player_level) as diff_min_player_level,
    (t.team_max_player_level - e.enemy_max_player_level) as diff_max_player_level,
    (t.team_std_player_level - e.enemy_std_player_level) as diff_std_player_level,

    (t.team_avg_brawler_power - e.enemy_avg_brawler_power) as diff_avg_brawler_power,
    (t.team_min_brawler_power - e.enemy_min_brawler_power) as diff_min_brawler_power,
    (t.team_max_brawler_power - e.enemy_max_brawler_power) as diff_max_brawler_power,

    (t.team_avg_brawler_total_trophies - e.enemy_avg_brawler_total_trophies) as diff_avg_brawler_total_trophies,
    (t.team_min_brawler_total_trophies - e.enemy_min_brawler_total_trophies) as diff_min_brawler_total_trophies,
    (t.team_max_brawler_total_trophies - e.enemy_max_brawler_total_trophies) as diff_max_brawler_total_trophies,
    (t.team_std_brawler_total_trophies - e.enemy_std_brawler_total_trophies) as diff_std_brawler_total_trophies,

    (t.team_avg_brawler_rank_on_account - e.enemy_avg_brawler_rank_on_account) as diff_avg_brawler_rank_on_account,
    (t.team_min_brawler_rank_on_account - e.enemy_min_brawler_rank_on_account) as diff_min_brawler_rank_on_account,
    (t.team_max_brawler_rank_on_account - e.enemy_max_brawler_rank_on_account) as diff_max_brawler_rank_on_account,
    (t.team_std_brawler_rank_on_account - e.enemy_std_brawler_rank_on_account) as diff_std_brawler_rank_on_account,

    (t.team_pct_highest_player_brawler - e.enemy_pct_highest_player_brawler) as diff_pct_highest_player_brawler

  from base b
  join team_agg t
    on  t.battletime = b.feature_battletime_ts
    and t.mode       = b.mode
    and t.map_key    = b.map_key
    and t.type       = b.type
    and t.duration   = b.duration
    and t.team_result = b.result
  join enemy_agg e
    on  e.battletime = b.feature_battletime_ts
    and e.mode       = b.mode
    and e.map_key    = b.map_key
    and e.type       = b.type
    and e.duration   = b.duration
    and e.enemy_team_result = b.enemy_result
)

insert into battle_features_3v3 (
  battletime, mode, type, duration, map_id, map, map_key,
  player_tag, player_name, brawler_name,
  win_label,
  player_total_trophies, player_level,
  brawler_power, brawler_total_trophies, brawler_rank_on_account,
  is_starplayer, is_highest_player_brawler,
  rank, trophychange,

  team_size,
  team_avg_player_total_trophies, team_std_player_total_trophies, team_max_player_total_trophies,
  team_avg_player_level, team_min_player_level, team_max_player_level, team_std_player_level,
  team_avg_brawler_power, team_min_brawler_power, team_max_brawler_power,
  team_avg_brawler_total_trophies, team_min_brawler_total_trophies, team_max_brawler_total_trophies, team_std_brawler_total_trophies,
  team_avg_brawler_rank_on_account, team_min_brawler_rank_on_account, team_max_brawler_rank_on_account, team_std_brawler_rank_on_account,
  team_pct_highest_player_brawler,
  team_comp_key,

  enemy_size,
  enemy_avg_player_total_trophies, enemy_std_player_total_trophies, enemy_max_player_total_trophies,
  enemy_avg_player_level, enemy_min_player_level, enemy_max_player_level, enemy_std_player_level,
  enemy_avg_brawler_power, enemy_min_brawler_power, enemy_max_brawler_power,
  enemy_avg_brawler_total_trophies, enemy_min_brawler_total_trophies, enemy_max_brawler_total_trophies, enemy_std_brawler_total_trophies,
  enemy_avg_brawler_rank_on_account, enemy_min_brawler_rank_on_account, enemy_max_brawler_rank_on_account, enemy_std_brawler_rank_on_account,
  enemy_pct_highest_player_brawler,
  enemy_comp_key,

  matchup_key,

  diff_avg_player_total_trophies, diff_std_player_total_trophies,
  diff_avg_player_level, diff_min_player_level, diff_max_player_level, diff_std_player_level,
  diff_avg_brawler_power, diff_min_brawler_power, diff_max_brawler_power,
  diff_avg_brawler_total_trophies, diff_min_brawler_total_trophies, diff_max_brawler_total_trophies, diff_std_brawler_total_trophies,
  diff_avg_brawler_rank_on_account, diff_min_brawler_rank_on_account, diff_max_brawler_rank_on_account, diff_std_brawler_rank_on_account,
  diff_pct_highest_player_brawler,

  updated_at
)
select
  battletime, mode, type, duration, map_id, map, map_key,
  player_tag, player_name, brawler_name,
  win_label,
  player_total_trophies, player_level,
  brawler_power, brawler_total_trophies, brawler_rank_on_account,
  is_starplayer, is_highest_player_brawler,
  rank, trophychange,

  team_size,
  team_avg_player_total_trophies, team_std_player_total_trophies, team_max_player_total_trophies,
  team_avg_player_level, team_min_player_level, team_max_player_level, team_std_player_level,
  team_avg_brawler_power, team_min_brawler_power, team_max_brawler_power,
  team_avg_brawler_total_trophies, team_min_brawler_total_trophies, team_max_brawler_total_trophies, team_std_brawler_total_trophies,
  team_avg_brawler_rank_on_account, team_min_brawler_rank_on_account, team_max_brawler_rank_on_account, team_std_brawler_rank_on_account,
  team_pct_highest_player_brawler,
  team_comp_key,

  enemy_size,
  enemy_avg_player_total_trophies, enemy_std_player_total_trophies, enemy_max_player_total_trophies,
  enemy_avg_player_level, enemy_min_player_level, enemy_max_player_level, enemy_std_player_level,
  enemy_avg_brawler_power, enemy_min_brawler_power, enemy_max_brawler_power,
  enemy_avg_brawler_total_trophies, enemy_min_brawler_total_trophies, enemy_max_brawler_total_trophies, enemy_std_brawler_total_trophies,
  enemy_avg_brawler_rank_on_account, enemy_min_brawler_rank_on_account, enemy_max_brawler_rank_on_account, enemy_std_brawler_rank_on_account,
  enemy_pct_highest_player_brawler,
  enemy_comp_key,

  matchup_key,

  diff_avg_player_total_trophies, diff_std_player_total_trophies,
  diff_avg_player_level, diff_min_player_level, diff_max_player_level, diff_std_player_level,
  diff_avg_brawler_power, diff_min_brawler_power, diff_max_brawler_power,
  diff_avg_brawler_total_trophies, diff_min_brawler_total_trophies, diff_max_brawler_total_trophies, diff_std_brawler_total_trophies,
  diff_avg_brawler_rank_on_account, diff_min_brawler_rank_on_account, diff_max_brawler_rank_on_account, diff_std_brawler_rank_on_account,
  diff_pct_highest_player_brawler,

  now()
from final

on conflict (battletime, mode, map_key, type, duration, player_tag)
do update set
  map_id = excluded.map_id,
  map = excluded.map,
  player_name = excluded.player_name,
  brawler_name = excluded.brawler_name,
  win_label = excluded.win_label,

  player_total_trophies = excluded.player_total_trophies,
  player_level = excluded.player_level,

  brawler_power = excluded.brawler_power,
  brawler_total_trophies = excluded.brawler_total_trophies,
  brawler_rank_on_account = excluded.brawler_rank_on_account,

  is_starplayer = excluded.is_starplayer,
  is_highest_player_brawler = excluded.is_highest_player_brawler,

  rank = excluded.rank,
  trophychange = excluded.trophychange,

  team_size = excluded.team_size,
  team_avg_player_total_trophies = excluded.team_avg_player_total_trophies,
  team_std_player_total_trophies = excluded.team_std_player_total_trophies,
  team_max_player_total_trophies = excluded.team_max_player_total_trophies,

  team_avg_player_level = excluded.team_avg_player_level,
  team_min_player_level = excluded.team_min_player_level,
  team_max_player_level = excluded.team_max_player_level,
  team_std_player_level = excluded.team_std_player_level,

  team_avg_brawler_power = excluded.team_avg_brawler_power,
  team_min_brawler_power = excluded.team_min_brawler_power,
  team_max_brawler_power = excluded.team_max_brawler_power,

  team_avg_brawler_total_trophies = excluded.team_avg_brawler_total_trophies,
  team_min_brawler_total_trophies = excluded.team_min_brawler_total_trophies,
  team_max_brawler_total_trophies = excluded.team_max_brawler_total_trophies,
  team_std_brawler_total_trophies = excluded.team_std_brawler_total_trophies,

  team_avg_brawler_rank_on_account = excluded.team_avg_brawler_rank_on_account,
  team_min_brawler_rank_on_account = excluded.team_min_brawler_rank_on_account,
  team_max_brawler_rank_on_account = excluded.team_max_brawler_rank_on_account,
  team_std_brawler_rank_on_account = excluded.team_std_brawler_rank_on_account,

  team_pct_highest_player_brawler = excluded.team_pct_highest_player_brawler,
  team_comp_key = excluded.team_comp_key,

  enemy_size = excluded.enemy_size,
  enemy_avg_player_total_trophies = excluded.enemy_avg_player_total_trophies,
  enemy_std_player_total_trophies = excluded.enemy_std_player_total_trophies,
  enemy_max_player_total_trophies = excluded.enemy_max_player_total_trophies,

  enemy_avg_player_level = excluded.enemy_avg_player_level,
  enemy_min_player_level = excluded.enemy_min_player_level,
  enemy_max_player_level = excluded.enemy_max_player_level,
  enemy_std_player_level = excluded.enemy_std_player_level,

  enemy_avg_brawler_power = excluded.enemy_avg_brawler_power,
  enemy_min_brawler_power = excluded.enemy_min_brawler_power,
  enemy_max_brawler_power = excluded.enemy_max_brawler_power,

  enemy_avg_brawler_total_trophies = excluded.enemy_avg_brawler_total_trophies,
  enemy_min_brawler_total_trophies = excluded.enemy_min_brawler_total_trophies,
  enemy_max_brawler_total_trophies = excluded.enemy_max_brawler_total_trophies,
  enemy_std_brawler_total_trophies = excluded.enemy_std_brawler_total_trophies,

  enemy_avg_brawler_rank_on_account = excluded.enemy_avg_brawler_rank_on_account,
  enemy_min_brawler_rank_on_account = excluded.enemy_min_brawler_rank_on_account,
  enemy_max_brawler_rank_on_account = excluded.enemy_max_brawler_rank_on_account,
  enemy_std_brawler_rank_on_account = excluded.enemy_std_brawler_rank_on_account,

  enemy_pct_highest_player_brawler = excluded.enemy_pct_highest_player_brawler,
  enemy_comp_key = excluded.enemy_comp_key,

  matchup_key = excluded.matchup_key,

  diff_avg_player_total_trophies = excluded.diff_avg_player_total_trophies,
  diff_std_player_total_trophies = excluded.diff_std_player_total_trophies,

  diff_avg_player_level = excluded.diff_avg_player_level,
  diff_min_player_level = excluded.diff_min_player_level,
  diff_max_player_level = excluded.diff_max_player_level,
  diff_std_player_level = excluded.diff_std_player_level,

  diff_avg_brawler_power = excluded.diff_avg_brawler_power,
  diff_min_brawler_power = excluded.diff_min_brawler_power,
  diff_max_brawler_power = excluded.diff_max_brawler_power,

  diff_avg_brawler_total_trophies = excluded.diff_avg_brawler_total_trophies,
  diff_min_brawler_total_trophies = excluded.diff_min_brawler_total_trophies,
  diff_max_brawler_total_trophies = excluded.diff_max_brawler_total_trophies,
  diff_std_brawler_total_trophies = excluded.diff_std_brawler_total_trophies,

  diff_avg_brawler_rank_on_account = excluded.diff_avg_brawler_rank_on_account,
  diff_min_brawler_rank_on_account = excluded.diff_min_brawler_rank_on_account,
  diff_max_brawler_rank_on_account = excluded.diff_max_brawler_rank_on_account,
  diff_std_brawler_rank_on_account = excluded.diff_std_brawler_rank_on_account,

  diff_pct_highest_player_brawler = excluded.diff_pct_highest_player_brawler,

  pred_win_prob = NULL,
  pred_explanation_tech = NULL,
  pred_explanation_player = NULL,
  model_version = NULL,
  pred_updated_at = NULL,

  updated_at = now();
"""


def refresh(days_back: int, chunk_hours: int) -> dict:
    if chunk_hours <= 0:
        raise ValueError("CHUNK_HOURS must be > 0")

    conn = get_conn()
    cur = conn.cursor()
    chunk_summaries = []
    try:
        now_utc = dt.datetime.now(dt.timezone.utc)
        window_start = now_utc - dt.timedelta(days=days_back)
        window_end = now_utc
        chunk_start = window_start

        while chunk_start < window_end:
            chunk_end = min(chunk_start + dt.timedelta(hours=chunk_hours), window_end)
            print(
                "[refresh_battle_features_3v3] chunk "
                f"start={chunk_start.isoformat()} end={chunk_end.isoformat()}"
            )
            cur.execute(UPSERT_SQL, (chunk_start, chunk_end))
            conn.commit()
            chunk_summaries.append(
                {
                    "start": chunk_start.isoformat(),
                    "end": chunk_end.isoformat(),
                }
            )
            chunk_start = chunk_end

        # ✅ battle_features_3v3.battletime is timestamptz, so no casts needed
        cur.execute(
            """
            select count(*)
            from battle_features_3v3
            where battletime >= now() - make_interval(days => %s)
            """,
            (days_back,)
        )
        rows_in_window = cur.fetchone()[0]

        cur.execute(
            """
            select count(*) from (
              select battletime, mode, map_key, type, duration, count(*) as n
              from battle_features_3v3
              where battletime >= now() - make_interval(days => %s)
              group by 1,2,3,4,5
            ) x
            """,
            (days_back,)
        )
        matches_in_window = cur.fetchone()[0]

        cur.execute(
            """
            select count(*) from (
              select battletime, mode, map_key, type, duration, count(*) as n
              from battle_features_3v3
              where battletime >= now() - make_interval(days => %s)
              group by 1,2,3,4,5
              having count(*) <> 6
            ) bad
            """,
            (days_back,)
        )
        bad_matches_not_6 = cur.fetchone()[0]

        return {
            "days_back": days_back,
            "chunk_hours": chunk_hours,
            "chunks": chunk_summaries,
            "rows_in_window": rows_in_window,
            "matches_in_window": matches_in_window,
            "bad_matches_not_6": bad_matches_not_6,
        }

    finally:
        cur.close()
        conn.close()


def main():
    out = refresh(DAYS_BACK, CHUNK_HOURS)
    print(f"[refresh_battle_features_3v3] done: {out} @ {dt.datetime.now()}")


if __name__ == "__main__":
    main()
