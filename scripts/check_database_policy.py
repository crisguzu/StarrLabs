#!/usr/bin/env python3
"""Fail CI if active code can bypass the shared local PostgreSQL policy."""

from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", ".venv", "venv", ".venv.broken", "__pycache__"}
RULES = {
    "direct psycopg2 connection": re.compile(r"psycopg2\s*\.\s*connect\s*\("),
    "hard-coded required SSL": re.compile(r"sslmode\s*=\s*['\"]require['\"]"),
    "Supabase hostname": re.compile(r"supabase\.co", re.IGNORECASE),
}


def main() -> int:
    violations = []
    for path in ROOT.rglob("*"):
        if path.resolve() == Path(__file__).resolve():
            continue
        if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
            continue
        if path.suffix not in {".py", ".yml", ".yaml"}:
            continue
        text = path.read_text(errors="replace")
        for label, pattern in RULES.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                violations.append("{}:{}: {}".format(path.relative_to(ROOT), line, label))
    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 1
    print("DATABASE_POLICY_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
