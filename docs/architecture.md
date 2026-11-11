# Source-backed architecture

The diagram summarizes the self-hosted source implementation, not a certified production deployment. Arrows show principal application/data relationships, not every request and response.

- **External game/reference inputs:** `services/player_verification.py`, `services/reference_collector.py`, `services/brawlapi_reference_collector.py`. API access and third-party terms remain the operator’s responsibility.
- **Bounded collection:** `services/collector.py`, `services/profile_updater.py`, `services/opponent_profile_collector.py`, `services/scout_profile_retry.py`.
- **Persistence:** `database.py` implements bounded PostgreSQL connections; `schema/0000_baseline.sql` plus `migrations/` now pass an isolated PostgreSQL 17 empty-schema check; focused application acceptance also passes; see VERIFICATION.md for scope.
- **Derived analytics / ML:** `services/victory_score_calculation.py`, `models/refresh_battle_features_3v3.py`, `models/predict_victory_model.py` and separate training/evaluation scripts. Scoring outputs are stored in PostgreSQL and read by the application. No private trained model is distributed.
- **Web application:** `main.py`, `templates/`, `static/styles/`; FastAPI routes render Jinja2 pages and expose application APIs.
- **Identity adapters:** `services/email_auth.py`, `services/player_verification.py`; operator-configured Resend/Turnstile credentials and own selected player accounts.
- **Pipeline coordination:** `scripts/run_data_pipeline_job.py` explicitly selects jobs and serializes mutations with a lock. There is no installed scheduler in this export.

## Deployment trust boundaries

Application identity uses its own opaque email sessions and verified player ownership; Cloudflare identity headers and development overrides do not grant application identity. Legacy private operator/PoC routes return 404 in this distribution. Exact Host/Origin configuration and HTTPS settings remain the self-hosting operator’s responsibility. Focused fresh-schema, backend and Chromium frontend checks pass, but no source-level test certifies real provider delivery, Safari compatibility, remote ingress, operational security or tested backups. See VERIFICATION.md for the accepted scope and remaining limitations.

## Decisions, tradeoffs and failure paths

- **Single worker and bounded database access rather than a distributed queue:** database.py admits connections through a bounded pool; the README supports one application worker. Operational capacity cannot be multiplied by launching more workers and assuming shared budgets are sufficient.
- **Raw observations plus derived projections:** services/collector.py retains content-hashed battlelog payloads and observation links, while battle projections upsert by battle time/player. Migration 0013 defines raw-payload uniqueness separately from the mutable last-observed metadata. This makes raw replay inspectable but requires explicit retention and metric semantics; it does not establish a universal event-uniqueness guarantee.
- **Point-in-time context rather than future information:** migration 0005 selects only timestamped profile snapshots at or before battle time. Missing eligible context stays missing; this boundary is not evidence of prediction accuracy or complete historical coverage.
- **Admission before paid work:** request_budget.py uses a transactional advisory lock and returns 429 when a budget is exhausted. database.py bounds checkout waiting. Provider timeouts/retries in services/collector.py do not replace operator quotas, consent or retention decisions.
- **Empty-schema bootstrap rather than implicit upgrade:** scripts/init_db.py refuses a non-empty public schema. Migrations and backup/restore must be reviewed explicitly for existing data. The no-sync disposable schema test validates installation, not production durability.

[Verification scope and exact schema evidence](VERIFICATION.md) separate schema, app, policy, browser, synthetic and unverified live-provider behavior.
