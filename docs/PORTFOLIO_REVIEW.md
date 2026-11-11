# StarrLabs — candidate rubric and release review

Review date: 2026-11-10. Reviewer: MISSY; subjective engineering assessment using the same ten-category rubric,not hiring/security/legal certification. **This document is a review artifact,not execution authority.**

## Revision/state distinction

Original audited revision d7049eac23a582a6016e422ac91790d55f901d76 scored **73/100**. Public main at the start of this review (before this PR merge) baacb785515754a45e6750f45cb21a5e1c4c2775 scores **77/100** after the merged documentation-only batch. The **reviewed candidate source** code/probe revision de5e9caafb3303a7ca0fd15909dbc05641b3f8d1 scores **84/100**, including the later hash-matched bounded measurement receipt. Documentation/results commits may follow; executed probe and underlying code hashes are in the receipt. One performance point added for narrow evidence,not production scale.

## Ten-category scorecard

| Category | Main | Candidate/max | Evidence and remaining limit |
|---|---:|---:|---|
| Target-role relevance | 9 | 9/10 | [README.md:13](https://github.com/crisguzu/StarrLabs/blob/de5e9caafb3303a7ca0fd15909dbc05641b3f8d1/README.md#L13) ties the project to gaming analytics; no real cloud-infrastructure operating experience implied. |
| Original engineering & decisions | 14 | 14/15 | [docs/architecture.md:17](https://github.com/crisguzu/StarrLabs/blob/de5e9caafb3303a7ca0fd15909dbc05641b3f8d1/docs/architecture.md#L17) explains pool,raw/projection,temporal,admission and bootstrap tradeoffs; production outcomes still unmeasured. |
| Data architecture, schemas & scale | 11 | 13/15 | [docs/DATA_CONTRACT.md:9](https://github.com/crisguzu/StarrLabs/blob/de5e9caafb3303a7ca0fd15909dbc05641b3f8d1/docs/DATA_CONTRACT.md#L9) and [examples/data_product/metrics.sql:2](https://github.com/crisguzu/StarrLabs/blob/de5e9caafb3303a7ca0fd15909dbc05641b3f8d1/examples/data_product/metrics.sql#L2) define grains,draw denominator,missing context and temporal exclusions. Equal-timestamp production tie policy remains unresolved. |
| Reproducibility / DX | 6 | 8/10 | [docs/DATA_CONTRACT.md:25](https://github.com/crisguzu/StarrLabs/blob/de5e9caafb3303a7ca0fd15909dbc05641b3f8d1/docs/DATA_CONTRACT.md#L25) provides credential-free actual-schema demo; full real-provider sign-in/delivery remains unverified. |
| Tests & verification | 6 | 8/10 | [tests/test_data_product.py:12](https://github.com/crisguzu/StarrLabs/blob/de5e9caafb3303a7ca0fd15909dbc05641b3f8d1/tests/test_data_product.py#L12) and [.github/workflows/ci.yml:6](https://github.com/crisguzu/StarrLabs/blob/de5e9caafb3303a7ca0fd15909dbc05641b3f8d1/.github/workflows/ci.yml#L6) gate focused tests/schema/metric correctness. Portable browser packaging is not a passing current rerun. |
| Security, privacy & permissions | 8 | 8/10 | [THIRD_PARTY_NOTICES.md:23](https://github.com/crisguzu/StarrLabs/blob/de5e9caafb3303a7ca0fd15909dbc05641b3f8d1/THIRD_PARTY_NOTICES.md#L23) and test_access_policy.py preserve default-off ownership boundaries; no exhaustive dependency/remote-security certification. |
| Performance & reliability | 5 | 6/10 | [docs/PERFORMANCE.md:3](https://github.com/crisguzu/StarrLabs/blob/de5e9caafb3303a7ca0fd15909dbc05641b3f8d1/docs/PERFORMANCE.md#L3) now includes a hash-matched 1,000-row actual-schema SQL microprobe,raw samples and EXPLAIN. One point added; client startup,seven-sample tail,no-sync and non-production limits remain explicit. |
| README / reviewer experience | 9 | 9/10 | [README.md:11](https://github.com/crisguzu/StarrLabs/blob/de5e9caafb3303a7ca0fd15909dbc05641b3f8d1/README.md#L11) gives quickstart/verification/architecture plus actual synthetic UI; still a complex runtime installation. |
| Honest maturity | 5 | 5/5 | [docs/SCHEMA.md:5](https://github.com/crisguzu/StarrLabs/blob/de5e9caafb3303a7ca0fd15909dbc05641b3f8d1/docs/SCHEMA.md#L5) reconciles schema count; provider and synthetic limitations remain explicit. |
| Ownership / resume traceability | 4 | 4/5 | [README.md:9](https://github.com/crisguzu/StarrLabs/blob/de5e9caafb3303a7ca0fd15909dbc05641b3f8d1/README.md#L9) and PROVENANCE.md identify assisted-development/public-edition scope,not solo authorship of every component. |
| **Total** | **77** | **84/100** | Owner authorized CI-gated merge; open gates remain,not all-gates-cleared. |

## Verification evidence

The preceding source head 7016a1e had 25 focused tests and actual PostgreSQL 17.11 schema/data-product CI PASS ([run 42950204358](https://github.com/crisguzu/StarrLabs/actions/runs/42950204358)). Four new probe-contract tests pass locally without executing the probe. Latest exact-head CI must be read after this documentation update; no stale success is relabeled as a new run.

## Release gates

1. **Privacy — PASS for recorded inspected scope only:** prior source/history pattern scans and synthetic-only fixtures; not exhaustive semantic,binary,issue/comment or unadvertised-object clearance. New probe paths were reviewed for credential/private-locator patterns.
2. **Evidence-based claims — PASS for documented scope:** schema count reconciled; no benchmark result asserted. The bounded probe passed its stated assertions; no provider/web/durable-write performance claim follows.
3. **Permissions — PASS for previously inspected code/neutral-asset distribution,not legal certification:** retain third-party notices and provider/media terms; optional remote media remains default-off.
4. **Reproducible baseline — PASS limited:** actual isolated-schema/four-row data-product CI passes; full fresh browser/provider flow is NOT certified. The bounded SQL microprobe passed; not a real-provider performance test.
5. **Bounded security — PASS local/default scope:** unchanged policy tests/single-worker/default-off behavior. Remote production ingress and comprehensive security review UNKNOWN.
6. **Honest portfolio — PASS:** synthetic normalized adapter is not the real collector; simulated503 telemetry is not provider retry testing. Owner authorized CI-gated merge; not a release certification or closure of unrelated gates.
**Additional acceptance gate: BLOCKED/UNKNOWN browser rerun.** TasksMax 32 reached at launch; zero page cases. No sandbox bypass or limit increase. Prior 18-case result is historical.

## Step 5: bounded synthetic measurements completed

[Performance protocol,actual raw samples and explicit unknowns](PERFORMANCE.md). Owner authorized the idle-worker pause. The worker had no active job and was frozen only while both probes ran serially; its original process resumed after 1.0524 seconds. Owned unit exited,owned PostgreSQL/scratch were removed and restoration was independently checked. Combined peak RAM 111607808 bytes / 11 tasks; not per-probe resource figures. Peak scratch bytes were not sampled; no directory hard quota is claimed.

## Resume-safe wording

Prepared a self-hosted game-data analytics edition with versioned PostgreSQL schemas,ownership-aware access controls and a reproducible synthetic SQL case study covering duplicate observations,late data and point-in-time context. Report observed CI tests separately from provider/production claims.

Do not claim production volume,p50/p95/SLA,complete security/legal certification,real-provider E2E or employer outcomes from these synthetic personal projects.

## Prioritized next gates

- P0: resolve remaining permissions/admission limits with owner evidence,not score arithmetic.
- P1: owner authorized CI-gated merge in request 1569926720302092309; require exact-head success and keep remaining gates explicit.
- P2: retain hash-bound measurements. Any broader or repeated performance experiment needs its own reviewed workload/admission; do not extrapolate into capacity/SLA claims.
