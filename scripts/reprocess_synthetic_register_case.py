"""Safely rebuild a single synthetic case graph from its stored CSV registers.

Usage: python scripts/reprocess_synthetic_register_case.py --case-id ...
Add --apply to write the rebuilt graph. The case and source documents are kept;
only derived entities/relationships for the explicitly selected synthetic case
are replaced. A SQLite backup is created before any write.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import local_store


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--expected-marker", required=True, help="Required source text marker used to scope the migration")
    parser.add_argument("--apply", action="store_true", help="Apply the rebuild after validating scope and creating a backup")
    args = parser.parse_args()
    case_id = args.case_id
    with local_store.connect() as conn:
        case = conn.execute("SELECT title FROM cases WHERE case_id=?", (case_id,)).fetchone()
        docs = conn.execute("SELECT filename,content FROM documents WHERE case_id=?", (case_id,)).fetchall()
        before_entities = conn.execute("SELECT count(*) FROM entities WHERE case_id=?", (case_id,)).fetchone()[0]
        before_relationships = conn.execute("SELECT count(*) FROM relationships WHERE case_id=?", (case_id,)).fetchone()[0]
    if not case:
        parser.error(f"Case {case_id!r} does not exist")
    combined = "\n".join(row["content"] for row in docs)
    if args.expected_marker.casefold() not in combined.casefold():
        parser.error("Expected marker is absent; refusing to modify this case")
    if not any("entity_register" in row["filename"].casefold() for row in docs) or not any("relationship_register" in row["filename"].casefold() for row in docs):
        parser.error("Both entity and relationship register documents are required; refusing to modify this case")
    print(f"Validated scoped synthetic case {case_id} ({case['title']})")
    print(f"Current derived rows: {before_entities} entities, {before_relationships} relationships; {len(docs)} source documents")
    if not args.apply:
        print("Dry run only. Add --apply to rebuild this case. No data was changed.")
        return 0
    backup = local_store.DB_PATH.with_name(f"{local_store.DB_PATH.stem}.before-register-reprocess-{datetime.now():%Y%m%d-%H%M%S}.sqlite3")
    source = local_store.connect()
    destination = __import__("sqlite3").connect(backup)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    try:
        result = local_store.reprocess_registered_case(case_id, args.expected_marker)
    except Exception:
        print(f"Rebuild failed. The pre-change backup was retained at {backup}")
        raise
    print(f"Backup: {backup}")
    print(f"Applied: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
