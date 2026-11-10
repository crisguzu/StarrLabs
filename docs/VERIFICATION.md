# Verification scope and limitations

## Accepted backend checks

Accepted again on 2026-11-10 in a fresh isolated Linux/Python 3.12/PostgreSQL 17 environment: locked minimal dependency install; actual initializer and all migrations; public/basic/profile pages; correct MIME local artwork; verified-owner list read/create; cross-user read/rename denial; anonymous lookup/private read denial; private share metadata/content 404; explicitly public share reads 200; bounded result parameters; default-off collection and club sync; simultaneous transactional request budgets; simultaneous email-budget reservation with mocked challenge/email providers; zero external HTTP calls. The owned disposable database exited and its RAM-backed scratch was removed.

Fifteen focused authorization-policy unit tests pass. These focused checks are not exhaustive vulnerability, legal, deployment or backup certification.

## Accepted browser checks

Chromium 134.0.6998.35, driven by integrity-verified Playwright Core 1.51.1, passed 18 checks at 1440x900 and 390x844. Each viewport covered six captured application pages (registry, login, guide, about, event and a fictional profile), email-form transitions, synthetic verified-owner restoration and anonymous public-share display. No document overflow, broken images, unhandled JavaScript errors or page-initiated external requests were observed. Local chart code and profile period controls loaded; login focus, rejected-code feedback and return-to-email behavior passed.

HTML came from the accepted real application test above; browser API responses were synthetic fixtures. This combines real backend checks with focused frontend checks; it is **not** a live authenticated provider-to-browser end-to-end test. No real player/account records were used. Event artwork references discovered during the first pass were replaced with bundled original neutral SVGs, then the application and affected browser checks were rerun successfully.

The Chromium sandbox remained enabled. Browser limits were 512 MiB RAM, one CPU and 128 tasks/threads, with 30-second job and 20-second per-process CPU limits. The successful browser job recorded 71 peak tasks/threads and approximately 4.81 CPU seconds; its memory peak reached the enforced 512 MiB ceiling. Owned browser processes exited and RAM-backed scratch was removed.

## Not verified

A focused mobile WebKit check was attempted but could not launch because its host lacks libharfbuzz-icu.so.0. No system packages were changed and no sandbox was disabled. WebKit/Safari compatibility and physical iPhone behavior remain unverified. Real game/email/Turnstile provider delivery, remote HTTPS deployment, optional ML training and production security certification are outside this acceptance. Operators must validate their own provider configuration, identity journey, retention, backups and ingress before deployment.

## Reproduce backend checks

Install requirements-test.txt. Set POSTGRES_BIN to your PostgreSQL 17 binary directory, and STARRLABS_TEST_SCRATCH to a protected executable RAM-backed directory on your Linux test host (or use XDG_RUNTIME_DIR). Then run tests/schema_smoke.py and tests/app_smoke.py with explicit memory, CPU, task, per-file and wall limits. No existing database is connected to; the disposable server listens on a protected Unix socket, not a network interface. The no-sync test setting is not a durability/backup proof. The application smoke also writes fictional rendered pages under ignored .smoke-rendered/ for independent frontend inspection.
