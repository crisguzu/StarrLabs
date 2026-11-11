# Portable synthetic browser checks

The 18 assertions in browser_accept.cjs cover six actual captured app pages plus mocked email transitions, owner restoration and public sharing, at desktop 1440x900 and mobile 390x844. This is captured-HTML plus synthetic API testing, NOT live backend/provider E2E. All page-initiated non-loopback requests are rejected, Chromium sandbox remains enabled and JavaScript errors/broken images/layout overflow fail the run.

Prerequisites: Linux Python 3.12, PostgreSQL 17 binaries and Node 22. Generate fresh HTML using the repository's isolated application acceptance runner first; it installs requirements-test.txt into disposable scratch and tests the real app with synthetic identities. It requires network access to the Python package index, but no provider/account credentials. It creates .smoke-rendered pages and version/log artifacts, never a live deployment.

~~~sh
POSTGRES_BIN=/usr/lib/postgresql/17/bin STARRLABS_TEST_SCRATCH=/tmp python3 tests/app_smoke.py
npm install --no-save --package-lock=false playwright-core@1.51.1
npx playwright-core install chromium
node tests/browser_accept.cjs
~~~

Expected stdout: passed=true,cases=18; inspect .browser-results/browser.json and screenshot artifacts. Do not commit captures or dependency directories by default. Optional TEST_ROOT/TEST_OUT select input/output directories; PW_PACKAGE selects an existing playwright-core package and CHROMIUM_EXECUTABLE selects an existing compatible binary. These overrides do not disable the sandbox. No --no-sandbox fallback is supported. The fixture tags and emails are synthetic, not public account records.

This harness is portable source packaging of the prior accepted 18-case harness. A rerun must cite exact input hashes and actual exit/results. WebKit needs separate installed system dependencies; physical Safari remains unverified. CI currently gates 25 focused Python tests and the PostgreSQL walkthrough; this browser runner is an explicit opt-in command, not claimed as an automated CI gate.

## Current rerun limitation

The steps 3–4 rerun verified playwright-core 1.51.1 package integrity, but Chromium failed at launch with pthread_create EAGAIN and pids.peak=32 at the admitted TasksMax=32 boundary. Zero page cases ran; this is NOT a current 18-case pass. No sandbox workaround or unapproved limit increase was attempted. Owned browser/package scratch was removed. Prior successful 18-case evidence remains historical only. A separately reviewed sufficient task/thread budget is needed before another browser run.
