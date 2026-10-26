"""Persistent opaque login sessions and case ownership checks for the local store."""
import hashlib
import secrets
import time
from contextvars import ContextVar
from fastapi import HTTPException

current_officer = ContextVar("current_officer", default=None)


def issue_session(badge):
    from backend import local_store
    token = secrets.token_urlsafe(32)
    with local_store.connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS officer_sessions(token_hash TEXT PRIMARY KEY,badge TEXT NOT NULL,expires REAL NOT NULL)")
        conn.execute("INSERT INTO officer_sessions VALUES(?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), badge, time.time() + 86400))
    return token


def resolve_session(token):
    if not token: return None
    from backend import local_store
    with local_store.connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS officer_sessions(token_hash TEXT PRIMARY KEY,badge TEXT NOT NULL,expires REAL NOT NULL)")
        row = conn.execute("SELECT badge FROM officer_sessions WHERE token_hash=? AND expires>?", (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
    return row["badge"] if row else None


def may_read(metadata):
    owner = metadata.get("uploaded_by")
    return not owner or str(owner).casefold() == str(current_officer.get() or "").casefold()


def require_case(case_id):
    import json
    from backend import local_store
    with local_store.connect() as conn:
        row = conn.execute("SELECT metadata FROM cases WHERE case_id=?", (case_id,)).fetchone()
    if row and not may_read(json.loads(row["metadata"])):
        raise HTTPException(status_code=403, detail="You do not have access to this case.")
