#!/usr/bin/env python3
"""Phase E registry controls. Fail closed for shadowing and promotion."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import connect
from models.loadout_evaluation_contract import promotion_gate_passes


def start_shadow(evaluation_run_id: int, actor: str) -> dict:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT status,promotion_eligible,artifact_path FROM loadout_model_evaluation_runs WHERE id=%s", (evaluation_run_id,))
        row = cur.fetchone()
        if not row or row[0] != "complete" or not row[1] or not row[2]:
            return {"started": False, "reason": "evaluation is not complete and promotion-eligible"}
        artifact_dir = Path(row[2]).resolve()
        artifact = artifact_dir / "stage4.joblib"
        if not artifact.is_file() or ROOT not in artifact.parents:
            return {"started": False, "reason": "reviewed artifact is unavailable or outside the repository"}
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        version = f"loadout-v1-eval-{evaluation_run_id}"
        cur.execute(
            """INSERT INTO loadout_model_registry
               (model_version,evaluation_run_id,status,artifact_path,artifact_sha256,shadow_started_at)
               VALUES (%s,%s,'shadow',%s,%s,now()) RETURNING id""",
            (version, evaluation_run_id, str(artifact), digest),
        )
        registry_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO loadout_promotion_audit
               (model_registry_id,action,actor,reason,evidence)
               VALUES (%s,'shadow_start',%s,%s,%s::jsonb)""",
            (registry_id, actor, "evaluation passed; begin isolated shadow scoring",
             json.dumps({"evaluation_run_id": evaluation_run_id, "artifact_sha256": digest})),
        )
        return {"started": True, "model_version": version, "registry_id": registry_id,
                "artifact_sha256": digest}


def promote(model_version: str, actor: str, explicit_approval: bool, reason: str) -> dict:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT r.id,e.promotion_eligible,r.shadow_started_at,
                      coalesce(extract(epoch FROM (now()-r.shadow_started_at))/86400,0),
                      count(s.*),count(s.*) FILTER (WHERE s.outcome IS NOT NULL)
               FROM loadout_model_registry r
               JOIN loadout_model_evaluation_runs e ON e.id=r.evaluation_run_id
               LEFT JOIN loadout_shadow_scores s ON s.model_registry_id=r.id
               WHERE r.model_version=%s AND r.status='shadow'
               GROUP BY r.id,e.promotion_eligible,r.shadow_started_at""",
            (model_version,),
        )
        row = cur.fetchone()
        if not row:
            return {"promoted": False, "reason": "no shadow model with that version"}
        registry_id, eligible, started_at, shadow_days, scores, outcomes = row
        shadow_healthy = scores > 0 and outcomes > 0
        passed = promotion_gate_passes(
            evaluation_eligible=eligible, shadow_days=float(shadow_days),
            shadow_healthy=shadow_healthy, explicit_approval=explicit_approval,
        )
        evidence = {"shadow_started_at": started_at, "shadow_days": float(shadow_days),
                    "scores": scores, "outcomes": outcomes, "shadow_healthy": shadow_healthy}
        cur.execute(
            """INSERT INTO loadout_promotion_audit
               (model_registry_id,action,actor,explicit_approval,reason,evidence)
               VALUES (%s,%s,%s,%s,%s,%s::jsonb)""",
            (registry_id, "promote" if passed else "promotion_reject", actor,
             explicit_approval, reason, json.dumps(evidence, default=str)),
        )
        if passed:
            cur.execute("UPDATE loadout_model_registry SET status='promoted',promoted_at=now() WHERE id=%s", (registry_id,))
        return {"promoted": passed, "reason": None if passed else "promotion gates not satisfied", **evidence}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    shadow = sub.add_parser("start-shadow")
    shadow.add_argument("evaluation_run_id", type=int)
    shadow.add_argument("--actor", required=True)
    promotion = sub.add_parser("promote")
    promotion.add_argument("model_version")
    promotion.add_argument("--actor", required=True)
    promotion.add_argument("--reason", required=True)
    promotion.add_argument("--explicit-approval", action="store_true")
    args = parser.parse_args()
    result = (start_shadow(args.evaluation_run_id, args.actor) if args.command == "start-shadow"
              else promote(args.model_version, args.actor, args.explicit_approval, args.reason))
    print("LOADOUT_PHASE_E|" + json.dumps(result, default=str, sort_keys=True))
    return 0 if result.get("started") or result.get("promoted") else 2


if __name__ == "__main__":
    raise SystemExit(main())
