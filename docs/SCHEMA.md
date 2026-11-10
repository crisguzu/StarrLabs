# Empty-database bootstrap

The baseline contains **16 table definitions and no player/account rows**. It was recovered with PostgreSQL’s schema-only/no-owner/no-privileges/no-comments export from a retained pre-migration archive. Only reviewed schema DDL is included; the data-bearing archive is not part of this repository. Client session directives and psql meta-commands were removed.

The baseline plus all 23 numbered migrations passed a disposable PostgreSQL 17.10 check, yielding **58 public tables**, zero battles and zero player-profile records. Test inputs were hash-verified against the isolated execution workspace. No existing database was connected to or modified. This validates schema installation, not the complete web application, dependency installation or production deployment.

`python scripts/init_db.py --confirm-empty` is intended for a dedicated empty database with operator-owned credentials in `.env`. It refuses a non-empty public schema, uses an advisory lock, creates the baseline and applies migrations transactionally. The Python initializer has now also passed an isolated application smoke against a new PostgreSQL 17 database with freshly installed minimal dependencies. Full authorization, provider and rendered-UI acceptance remain separate gates.

Legacy installation-specific role grants now target the current bootstrap user. Conditional legacy-role checks do not require those roles to exist. Operators must design any separate runtime/backup/monitor grants explicitly; this preparation does not install database roles, services or schedules.

The reproducible SQL check is `POSTGRES_BIN=/path/to/postgresql/17/bin python tests/schema_smoke.py` on a private Linux test host. It uses a new protected RAM-backed directory, Unix sockets only, no TCP listener and a normally terminated owned server. Run it with explicit CPU, memory, PID, file and wall-time limits; it does not supply a general job runner. Its disposable no-sync initialization is not a durability/backup proof.
