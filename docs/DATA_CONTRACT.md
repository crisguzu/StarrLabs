# Data-product contract and synthetic walkthrough

## Scope and lineage

This example loads **fictional normalized observations**, not official provider payloads, into the actual 24-migration PostgreSQL schema. It exercises actual unique constraints and the actual battle_profile_context view. The small adapter in examples/data_product/demo.py is NOT services/collector.py and is not a substitute for a real-provider collector/retry acceptance test. No game account, API key, SMTP, live database or model is used.

Lineage: fixture.json â strict demo validation â collector_polls + content-addressed battlelog_payloads + per-poll observations â battles player-perspective projection â existing battle_profile_context â metrics.sql â expected JSON assertions. Source migrations 0002/0005/0013 own the real contracts; the fixture is a narrower synthetic adapter contract.

## Grains and keys

- Poll: one synthetic attempt per observation, plus one synthetic 503 failure. Failed attempts create no battle payload or projection.
- Raw content: UNIQUE(observed_from_tag, collection_kind, payload_hash). The demo hashes canonical sorted normalized JSON (player_tag,event_time,result), excluding observation time. A repeated poll reuses the payload and preserves a separate observation link. It does not claim that the production provider-payload hash uses this narrower schema. last_observed_at is mutable metadata, not immutable payload content.
- Observation: one source_poll_id per link to a payload, enforced by the actual table primary key.
- Battle: PRIMARY KEY(battletime,player_tag), a player perspectiveânot globally one row per match. Demo conflicts with differing outcomes are rejected before SQL; identical observations update last_observed_at without inflating the denominator. This narrow policy does not add conflict reconciliation to the production collector.
- Profile: timestamped snapshots per player. Actual migration 0005 selects latest captured_at <= battletime_ts and historical_precision='timestamped'. current_only legacy snapshots are ineligible. Missing historical context remains NULL. The demo rejects identical player/capture timestamps as ambiguous; the production view does not define an equal-timestamp tie-break and may require a separately reviewed policy.

## Time, completeness and metrics

All demo times are UTC Z. event_time describes gameplay; observed_at describes arrival. The 10:30 draw arrives at 13:00 after the 11:00 defeat; it is included by event time, not excluded because it arrived late. There is no watermark/finalization claim. Recomputing this tiny metric includes all fixture events. Live API history can have gaps; this fixture proves neither complete provider coverage nor retention compliance.

**Decisive win rate:** victories / (victories + defeats), evaluated across deduplicated player-perspective rows in this fixture. Draws excluded from this denominator only; no smoothing or predictive interpretation. Zero decisive denominator returns SQL NULL, not 0. **Context coverage:** rows with an eligible historical snapshot / all battle rows; missing context is counted in denominator. No freshness-age cutoff is asserted.

Expected: 5 successful observations, 6 polls, 4 unique raw payloads, 4 battles, 3 decisive outcomes, 2 victories, win_rate 0.666667; 3 contextualized rows, coverage 0.75. The future 12:00 profile (9999 trophies) must never enrich an earlier event. DEMO-B has no eligible snapshot and remains NULL.

## Run on Linux as a non-root user

Prerequisite: PostgreSQL 17 server/client binaries, Python 3.12 recommended. No Python third-party packages are needed for this walkthrough. From repository root:

~~~sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
POSTGRES_BIN=/usr/lib/postgresql/17/bin STARRLABS_TEST_SCRATCH=/tmp python3 examples/data_product/demo.py
~~~

If pg_config is on PATH and points to the intended installed PostgreSQL version, python3 examples/data_product/demo.py discovers its bindir automatically. Never point this at a live DATABASE_URL: the runner ignores that setting, creates its own mode 0700 cluster/socket, disables TCP listening, overrides PGHOST/PGPORT/PGUSER/PGDATABASE and removes its owned cluster on exit. initdb uses --no-sync for disposable tests, so no durability claim follows. The configured scratch parent must be trusted, writable and executable. Expect schema PASS followed by dataProduct PASS and matching metric JSON. Inspect actual output, not just a zero exit status.

The runner's SQL calls are separate psql sessions. Individual statements commit independently; the isolated disposable cluster is the safety boundary, not a multi-statement transaction. This is a correctness demonstration with 4 rows, not a benchmark or production-volume claim.

## Data quality checks

10 focused contract tests reject conflicts, unknown fields/results, unallowlisted identities, invalid ordering/timezones, ambiguous profiles, excessive rows and boolean versions. SQL acceptance covers actual duplicate constraints, late-arriving fixture inclusion, future-context exclusion, missing context, failed-poll non-ingestion and projection replay without count inflation. A recorded 503 is synthetic telemetry, not proof of live provider error handling/retry/backoff.
