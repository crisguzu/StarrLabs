# Empty-database bootstrap

The baseline contains **16 table definitions and no player/account rows**. It was recovered with PostgreSQL’s schema-only/no-owner/no-privileges/no-comments export from a retained pre-migration archive. Only reviewed schema DDL is included; the data-bearing archive is not part of this repository. Client session directives and psql meta-commands were removed.

On 2026-11-10, the baseline plus all **24 numbered migrations** through 0024_standalone_request_admission passed a disposable PostgreSQL **17.10** check, yielding **59 public tables** and zero player records. All 26 inputs (baseline, 24 migrations and schema test) were SHA-256 verified against source revision d7049ea before execution. Observed output: schema=PASS, tables=59, playerRecords=0, existingDatabaseConnections=0. The owned server exited normally and its protected RAM-backed scratch was removed. This supersedes the earlier 23-migration/58-table checkpoint; it validates schema installation, not complete application, provider, durability or production deployment acceptance.

`python scripts/init_db.py --confirm-empty` is intended for a dedicated empty database with operator-owned credentials in `.env`. It refuses a non-empty public schema, uses an advisory lock, creates the baseline and applies migrations transactionally. The Python initializer has now also passed an isolated application smoke against a new PostgreSQL 17 database with freshly installed minimal dependencies. Full authorization, provider and rendered-UI acceptance remain separate gates.

Legacy installation-specific role grants now target the current bootstrap user. Conditional legacy-role checks do not require those roles to exist. Operators must design any separate runtime/backup/monitor grants explicitly; this preparation does not install database roles, services or schedules.

The reproducible SQL check is `POSTGRES_BIN=/path/to/postgresql/17/bin python tests/schema_smoke.py` on a private Linux test host. It uses a new protected RAM-backed directory, Unix sockets only, no TCP listener and a normally terminated owned server. Run it with explicit CPU, memory, PID, file and wall-time limits; it does not supply a general job runner. Its disposable no-sync initialization is not a durability/backup proof.

[Latest schema input hashes and observed output](verification/schema-2026-11-10.json).
