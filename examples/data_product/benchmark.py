"""Opt-in synthetic SQL microprobe. Never imports production DB configuration."""
from pathlib import Path
import hashlib
import importlib.util
import json
import math
import os
import platform
import time

ROOT = Path(__file__).resolve().parents[2]
ROWS, SAMPLES, SEED = 1000, 7, 17

def nearest_rank(values, percentile):
    if not values or not 0 < percentile <= 1:
        raise ValueError("Nonempty samples and percentile in (0,1] required")
    return sorted(values)[math.ceil(len(values) * percentile) - 1]

def workload():
    return f"""
INSERT INTO public.player_profile_snapshots
(player_tag,captured_at,source,payload_hash,payload,trophies)
SELECT '#SYN-'||g, '2025-12-31T00:00:00Z'::timestamptz,
 'synthetic_benchmark', md5(g::text), '{{}}'::jsonb, 1000+g
FROM generate_series(0,9) AS g;
INSERT INTO public.battles(battletime,battletime_ts,player_tag,result,mode,legacy_profile_enrichment)
SELECT ('2026-01-01T00:00:00Z'::timestamptz + g*interval '1 second')::text,
 '2026-01-01T00:00:00Z'::timestamptz + g*interval '1 second',
 '#SYN-'||(g%10), CASE ((g+{SEED})%3) WHEN 0 THEN 'victory' WHEN 1 THEN 'defeat' ELSE 'draw' END,
 'synthetic_benchmark', false
FROM generate_series(1,{ROWS}) AS g;
ANALYZE public.battles;
ANALYZE public.player_profile_snapshots;
"""

QUERY = """SELECT json_build_object('battles',count(*),'context_rows',count(c.profile_snapshot_id),
'wins',count(*) FILTER(WHERE b.result='victory'),
'decisive',count(*) FILTER(WHERE b.result IN ('victory','defeat')))
FROM public.battles b JOIN public.battle_profile_context c
ON c.battletime=b.battletime AND c.player_tag=b.player_tag"""

def check(sql):
    statement = workload()
    started = time.perf_counter()
    sql(statement)
    insert_ms = (time.perf_counter() - started) * 1000
    expected = {'battles': ROWS, 'context_rows': ROWS,
                'wins': sum((g+SEED)%3 == 0 for g in range(1,ROWS+1)),
                'decisive': sum((g+SEED)%3 != 2 for g in range(1,ROWS+1))}
    assert json.loads(sql(QUERY)) == expected
    samples = []
    for _ in range(SAMPLES):
        started = time.perf_counter()
        assert json.loads(sql(QUERY)) == expected
        samples.append((time.perf_counter() - started) * 1000)
    plan = json.loads(sql('EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) ' + QUERY))
    sources = [ROOT/'tests/schema_smoke.py', Path(__file__), ROOT/'schema/0000_baseline.sql'] + sorted((ROOT/'migrations').glob('*.sql'))
    report = {'synthetic': True, 'scope': 'SQL projection bulk insert + actual temporal-view aggregate; NOT collector/API/web throughput',
              'rows': ROWS, 'profile_rows': 10, 'seed': SEED, 'samples': SAMPLES, 'warmup_queries': 1,
              'input_sql_bytes': len(statement.encode()), 'input_sql_sha256': hashlib.sha256(statement.encode()).hexdigest(),
              'python': platform.python_version(), 'platform': platform.system()+' '+platform.machine(),
              'postgres': sql('SHOW server_version'), 'results': expected,
              'insert_client_ms': insert_ms, 'insert_rows_per_client_second': ROWS/(insert_ms/1000),
              'query_client_ms': samples, 'query_client_p50_ms': nearest_rank(samples,.5),
              'query_client_p95_ms': nearest_rank(samples,.95),
              'battles_total_relation_bytes': int(sql("SELECT pg_total_relation_size('public.battles')")),
              'profile_total_relation_bytes': int(sql("SELECT pg_total_relation_size('public.player_profile_snapshots')")),
              'query_plan': plan,
              'source_hashes': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
              'limitations': ['unexecuted until exclusive admission', 'client samples include new psql process/session overhead',
                  '7 samples: nearest-rank p95 equals maximum, not a stable tail-latency estimate',
                  'warm cache, one host, one client, no concurrency', 'initdb --no-sync: no durable-write benchmark',
                  '1000 synthetic rows, no extrapolation to production scale', 'hardware/resource caps belong to job receipt']}
    # A result exists only after all assertions and SQL plan collection succeed.
    report['limitations'].remove('unexecuted until exclusive admission')
    print(json.dumps({'benchmark': 'PASS', **report}, sort_keys=True))

def main():
    if 'POSTGRES_BIN' not in os.environ:
        raise SystemExit('Set POSTGRES_BIN to reviewed PostgreSQL17 binaries; exclusive worker admission required')
    spec = importlib.util.spec_from_file_location('schema_smoke', ROOT/'tests/schema_smoke.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main(check=check)

if __name__ == '__main__':
    main()
