"""Synthetic adapter into real PostgreSQL schema; no app/provider/model calls."""
from datetime import datetime
from pathlib import Path
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

def timestamp(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("UTC Z timestamp required")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

def validate(fixture):
    if fixture.get("synthetic") is not True or type(fixture.get("version")) is not int or fixture["version"] != 1:
        raise ValueError("Unsupported synthetic contract")
    observations = fixture.get("observations", [])
    profiles = fixture.get("profiles", [])
    if not 1 <= len(observations) <= 100 or len(profiles) > 100:
        raise ValueError("Bounded demo fixture required")
    identities = {}
    for row in observations:
        if set(row) != {"player_tag", "event_time", "result", "observed_at"}:
            raise ValueError("Unknown/missing observation fields")
        if row["player_tag"] not in {"#DEMO-A", "#DEMO-B"}:
            raise ValueError("Only fictional allowlisted identities")
        if row["result"] not in {"victory", "defeat", "draw"}:
            raise ValueError("Unknown result")
        if timestamp(row["event_time"]) > timestamp(row["observed_at"]):
            raise ValueError("Observation precedes event")
        key = (row["player_tag"], row["event_time"])
        if key in identities and identities[key] != row["result"]:
            raise ValueError("Conflicting duplicate; explicit reconciliation required")
        identities[key] = row["result"]
    seen = set()
    for row in profiles:
        if set(row) != {"player_tag", "captured_at", "trophies"} or row["player_tag"] not in {"#DEMO-A", "#DEMO-B"}:
            raise ValueError("Invalid fictional profile")
        timestamp(row["captured_at"])
        key = (row["player_tag"], row["captured_at"])
        if key in seen or type(row["trophies"]) is not int or not 0 <= row["trophies"] <= 100000:
            raise ValueError("Ambiguous timestamp or invalid trophies")
        seen.add(key)
    failure = fixture["provider_failure"]
    if failure["status"] != 503:
        raise ValueError("Only the documented synthetic failure")
    timestamp(failure["observed_at"])
    return fixture

def quote(value):
    return "'" + str(value).replace("'", "''") + "'"

def check(sql):
    fixture = validate(json.loads((HERE / "fixture.json").read_text()))
    for row in fixture["profiles"]:
        payload = json.dumps(row, sort_keys=True)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        sql("INSERT INTO public.player_profile_snapshots(player_tag,captured_at,source,payload_hash,payload,trophies) VALUES(" +
            ",".join(map(quote, [row["player_tag"], row["captured_at"], "synthetic_demo", digest, payload, row["trophies"]])) + ")")
    for row in fixture["observations"]:
        poll = sql("INSERT INTO public.collector_polls(player_tag,endpoint,started_at,http_status,success) VALUES(" +
                   quote(row["player_tag"]) + ",'synthetic_fixture'," + quote(row["observed_at"]) + ",200,true) RETURNING id").splitlines()[0]
        payload = {k: row[k] for k in ("player_tag", "event_time", "result")}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        raw = sql("INSERT INTO public.battlelog_payloads(observed_from_tag,collection_kind,captured_at,last_observed_at,payload_hash,payload,returned_items) VALUES(" +
                  quote(row["player_tag"]) + ",'tracked'," + quote(row["observed_at"]) + "," + quote(row["observed_at"]) + "," + quote(digest) + "," + quote(encoded) + ",1) ON CONFLICT(observed_from_tag,collection_kind,payload_hash) DO UPDATE SET last_observed_at=EXCLUDED.last_observed_at RETURNING id").splitlines()[0]
        sql("INSERT INTO public.battlelog_payload_observations(source_poll_id,payload_id,observed_at) VALUES(" + poll + "," + raw + "," + quote(row["observed_at"]) + ")")
        sql("INSERT INTO public.battles(battletime,battletime_ts,player_tag,result,mode,raw_battle_hash,first_collected_at,last_observed_at,legacy_profile_enrichment) VALUES(" +
            ",".join(map(quote, [row["event_time"], row["event_time"], row["player_tag"], row["result"], "synthetic", digest, row["observed_at"], row["observed_at"]])) +
            ",false) ON CONFLICT(battletime,player_tag) DO UPDATE SET last_observed_at=EXCLUDED.last_observed_at")
    sql("INSERT INTO public.collector_polls(endpoint,started_at,http_status,success,error_category) VALUES('synthetic_fixture'," + quote(fixture["provider_failure"]["observed_at"]) + ",503,false,'synthetic_provider_failure')")
    observed = json.loads(sql((HERE / "metrics.sql").read_text()))
    if observed != fixture["expected"]:
        raise AssertionError({"observed": observed, "expected": fixture["expected"]})
    assert sql("SELECT count(*) FROM public.battle_profile_context WHERE profile_captured_at>battletime_ts") == "0"
    assert sql("SELECT count(*) FROM public.battle_profile_context WHERE profile_trophies=9999") == "0"
    assert sql("SELECT count(*) FROM public.battle_profile_context WHERE player_tag='#DEMO-B' AND profile_snapshot_id IS NULL") == "1"
    # Replaying the exact same projection cannot inflate its grain.
    before = sql("SELECT count(*) FROM public.battles")
    sql("INSERT INTO public.battles SELECT * FROM public.battles ON CONFLICT(battletime,player_tag) DO NOTHING")
    assert sql("SELECT count(*) FROM public.battles") == before
    print(json.dumps({"synthetic": True, "dataProduct": "PASS", "observed": observed, "checks": ["duplicate", "late_arrival", "no_future_context", "missing_context", "provider_503", "projection_replay"]}, sort_keys=True))

def main():
    if "POSTGRES_BIN" not in os.environ:
        pg = shutil.which("pg_config")
        if not pg:
            raise SystemExit("Install PostgreSQL 17 binaries or set POSTGRES_BIN; never supply a live DATABASE_URL")
        os.environ["POSTGRES_BIN"] = subprocess.check_output([pg, "--bindir"], text=True, timeout=2).strip()
    spec = importlib.util.spec_from_file_location("schema_smoke", ROOT / "tests/schema_smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main(check=check)

if __name__ == "__main__":
    main()
