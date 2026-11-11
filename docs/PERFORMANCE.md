# Performance: proposed probe, no measured result yet

**Status: PREPARED / EXECUTION BLOCKED.** No throughput, latency, storage, query-plan or production-scale result is claimed. The Alienware preflight found an existing independently polling worker; no competing job was launched and no unrelated service was stopped. An exclusive owner-reviewed run window is required. The fixture-contract tests do not execute this probe.

## Exact workload and command

examples/data_product/benchmark.py uses the existing isolated PostgreSQL schema runner. Fixed SQL generator: 1,000 player-perspective battles, 10 prior profile snapshots, seed 17. Results are grouped through the real battle_profile_context view; assertions check row counts/context/decisive outcomes. The measured insertion includes profile insert,battle bulk insert and ANALYZE, so rows/second is a client-observed bulk-SQL setup measurement, NOT API ingestion throughput.

After exclusive admission, Linux non-root user, PostgreSQL 17 binaries, trusted executable scratch:

~~~sh
POSTGRES_BIN=/usr/lib/postgresql/17/bin STARRLABS_TEST_SCRATCH=/tmp python3 examples/data_product/benchmark.py
~~~

Proposed job envelope: 45 seconds wall, 512 MiB RAM, one CPU, 32 tasks, 30 seconds per-process CPU, 16 MiB per-file, 32 MiB scratch reservation and 1 MiB retained log. These are requested quotas, not measured usage. Do not run on the Pi as a fallback, run inside CI by default, or claim an exclusive host without checking admission.

## Output and interpretation

After one warmup aggregate, seven serial query samples are emitted, with nearest-rank p50/p95. Each starts psql, includes a new local session and client-process overhead; p95 of seven samples equals the maximum and is not a stable tail estimate. EXPLAIN(ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON) separately records the server plan, buffers, planning and execution times. pg_total_relation_size includes relation indexes/TOAST allocation, not total retained cluster/WAL/dataset storage. SQL bytes,source hashes,row counts,seed,Python/platform/PostgreSQL version and raw samples accompany the output.

Caveats: warm cache,one host,one client,no concurrency,no remote-provider delivery. The disposable initdb uses --no-sync: this is not durable-write performance. The data-product's four-row correctness demo and its passing CI are not a substitute for measured performance. No extrapolation beyond the actual 1,000-row workload is justified. Actual receipt must also identify hardware,shared-load state,resource peaks,owned-process exit,scratch cleanup and output SHA-256 before publishing numbers.

## Production limits still unknown

Sustained ingress/API quota,bounded pool saturation,p50/p95 web/API latency,retention growth,concurrent writers,provider gaps,backup/restore throughput,real-user behavior and durability remain unmeasured. These are separate experiments,not inferred from a prepared generator.
