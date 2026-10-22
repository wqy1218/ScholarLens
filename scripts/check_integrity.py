"""Data-integrity self check.

Usage:
 python scripts/check_integrity.py <db_path>

Exit codes: 0 clean, 1 integrity problems, 2 missing or invalid path.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import store

if len(sys.argv) < 2:
    print(json.dumps({
        "ok": False,
        "error": "database path required: python scripts/check_integrity.py <db_path>",
    }, ensure_ascii=False, indent=2))
    sys.exit(2)

db = os.path.abspath(sys.argv[1])
if not os.path.isfile(db):
    print(json.dumps({
        "ok": False,
        "error": f"database not found: {db}",
    }, ensure_ascii=False, indent=2))
    sys.exit(2)

store.connect(db)
report = store.integrity_report()
print(json.dumps(report, ensure_ascii=False, indent=2))
sys.exit(0 if report["ok"] else 1)
