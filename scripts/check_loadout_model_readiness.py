#!/usr/bin/env python3
"""Fail closed unless the Phase C Loadout Availability evidence gate passes."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_data_pipelines import audit, _json_default


def main() -> int:
    result = audit(persist=False)
    payload = {
        "ready": result["item_model_ready"],
        "healthy": result["healthy"],
        "metrics": result["metrics"]["item_model"],
    }
    print("LOADOUT_MODEL_READINESS|" + json.dumps(payload, default=_json_default, sort_keys=True))
    return 0 if payload["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
