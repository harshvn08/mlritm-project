"""Non-destructive evidence import: source is read-only, target conflicts abort.

Run from the project root with server DATABASE_URL configured:
python -m backend.migrate_sqlite_to_postgres --source data/atlas_local.sqlite3 --users data/users.json
Repeat with --apply to commit. Never put a connection URL in command arguments.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
from backend.config import settings
from backend.postgres_store import connect, initialize


def migrate(source, users=None, apply=False):
    if settings.STORAGE_BACKEND != "postgres":
        raise ValueError("Set STORAGE_BACKEND=postgres and server DATABASE_URL before importing.")
    source = Path(source).resolve(strict=True)
    officers = json.loads(Path(users).read_text(encoding="utf-8")) if users else []
    with sqlite3.connect(source.as_uri()+"?mode=ro",uri=True) as original:
        original.row_factory = sqlite3.Row
        tables = {name:[dict(row) for row in original.execute(f"SELECT * FROM {name}")]
                  for name in ("cases","documents","entities","relationships")}
        # Preflight all original evidence before touching the target.
        for document in tables["documents"]:
            path = Path(document.get("storage_path") or "")
            if not path.is_file():
                raise ValueError(f"Original evidence missing for document {document['document_id']}; restore its backup, do not substitute extracted text.")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != document["sha256"]:
                raise ValueError(f"Evidence checksum mismatch for document {document['document_id']}")
            document["raw_content"] = raw
            document["storage_path"] = None
    counts = {name:len(rows) for name,rows in tables.items()}
    counts["officers"] = len(officers)
    initialize()  # Additive DDL only; never drops or truncates tables.
    class DryRun(Exception): pass
    try:
        with connect() as target:
            # Coordinate with normal case ingestion, in a stable order.
            for case in sorted(tables["cases"],key=lambda c:c["case_id"]):
                target.execute("SELECT pg_advisory_xact_lock(hashtextextended(?,0))",(case["case_id"],))
            for table, rows in tables.items():
                keys = {"cases":("case_id",),"documents":("document_id",),"entities":("case_id","entity_id"),"relationships":("relationship_id",)}[table]
                for row in rows:
                    where = " AND ".join(f"{key}=?" for key in keys)
                    existing = target.execute(f"SELECT * FROM {table} WHERE {where}",tuple(row[k] for k in keys)).fetchone()
                    if existing:
                        if any(existing.get(k) != v for k,v in row.items()):
                            raise ValueError(f"Target {table} conflict: import aborted without overwriting stored records.")
                        continue
                    columns = ",".join(row)
                    placeholders = ",".join("?" for _ in row)
                    target.execute(f"INSERT INTO {table}({columns}) VALUES({placeholders})",tuple(row.values()))
            for officer in officers:
                existing = target.execute("SELECT record FROM officers WHERE badge=?",(officer["badgeNumber"],)).fetchone()
                if existing:
                    if json.loads(existing["record"]) != officer:
                        raise ValueError("Officer conflict: import aborted without changing credentials.")
                else:
                    target.execute("INSERT INTO officers(badge,name_key,record) VALUES(?,?,?)",(officer["badgeNumber"],officer["officerName"].casefold(),json.dumps(officer)))
            if not apply: raise DryRun()
    except DryRun:
        pass
    return {"applied":apply,"records":counts,"source_preserved":True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",required=True)
    parser.add_argument("--users")
    parser.add_argument("--apply",action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(migrate(args.source,args.users,args.apply)))
    except Exception as exc:
        # Only our own validation errors are printable; never print driver secrets.
        parser.exit(1, (str(exc) if isinstance(exc, ValueError) else "Import failed; source preserved and target transaction rolled back.")+"\n")
