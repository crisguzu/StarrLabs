"""Disposable PostgreSQL 17 schema check; never connects to an existing cluster."""
from pathlib import Path
import json, os, shutil, subprocess, tempfile, time
ROOT = Path(__file__).resolve().parents[1]
BIN = Path(os.environ["POSTGRES_BIN"])

def main():
    if os.stat("/dev/shm").st_uid not in (0, os.getuid()):
        raise RuntimeError("Unexpected shared-memory mount owner")
    work = Path(tempfile.mkdtemp(prefix="starrlabs-schema-", dir=os.environ.get("STARRLABS_TEST_SCRATCH",os.environ.get("XDG_RUNTIME_DIR","/dev/shm"))))
    os.chmod(work, 0o700)
    data, sock = work / "data", work / "socket"
    sock.mkdir(mode=0o700)
    proc = None
    env = dict(os.environ, PGHOST=str(sock), PGPORT="55447", PGUSER="starrlabs", PGDATABASE="postgres", PGCONNECT_TIMEOUT="2")
    def sql(text):
        return subprocess.check_output([str(BIN / "psql"), "-X", "-v", "ON_ERROR_STOP=1", "-At", "-c", text], env=env, stderr=subprocess.STDOUT, timeout=5).decode().strip()
    try:
        subprocess.run([str(BIN / "initdb"), "-D", str(data), "-U", "starrlabs", "--auth-local=trust", "--auth-host=reject", "--no-sync"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=8)
        log = (work / "postgres.log").open("wb")
        proc = subprocess.Popen([str(BIN / "postgres"), "-D", str(data), "-k", str(sock), "-p", "55447", "-c", "listen_addresses=", "-c", "shared_buffers=8MB", "-c", "max_connections=8", "-c", "max_wal_size=32MB", "-c", "min_wal_size=32MB", "-c", "wal_level=minimal", "-c", "max_wal_senders=0"], stdout=log, stderr=log)
        for _ in range(30):
            if (sock / ".s.PGSQL.55447").exists():
                break
            if proc.poll() is not None:
                raise RuntimeError("Disposable PostgreSQL exited during startup: " + (work / "postgres.log").read_text(errors="replace")[-3000:])
            time.sleep(.1)
        sql((ROOT / "schema/0000_baseline.sql").read_text())
        sql("CREATE TABLE public.schema_migrations(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        for p in sorted((ROOT / "migrations").glob("*.sql")):
            try:
                sql(p.read_text())
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(p.name + ": " + exc.output.decode(errors="replace")[-3000:]) from None
        tables = int(sql("SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'"))
        assert int(sql("SELECT count(*) FROM public.battles")) == 0
        assert int(sql("SELECT count(*) FROM public.player_profiles")) == 0
        print(json.dumps({"schema": "PASS", "postgres": sql("SHOW server_version"), "tables": tables, "playerRecords": 0, "existingDatabaseConnections": 0}))
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        if proc is None or proc.poll() is not None:
            if (work / "postgres.log").exists():
                (ROOT / "schema-smoke-last.log").write_bytes((work / "postgres.log").read_bytes()[-16384:])
            shutil.rmtree(work)
            print("Owned disposable PostgreSQL exited; scoped scratch removed; bounded server log retained.")

if __name__ == "__main__":
    main()
