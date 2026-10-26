"""PostgreSQL transport for the existing relational evidence schema.

Only application-owned SQL is adapted; values always remain bound parameters.
Connections commit on success, roll back on failure, and close after each use.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from fastapi import HTTPException
from backend.config import settings

_active = ContextVar("postgres_transaction", default=None)


class Record(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


def record_factory(cursor):
    names = [column.name for column in cursor.description or ()]
    return lambda values: Record(zip(names, values))


class Connection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, parameters=()):
        if query == "BEGIN IMMEDIATE":
            query = "SELECT 1"  # psycopg starts the transaction automatically
        ignore = "INSERT OR IGNORE INTO" in query
        query = query.replace("INSERT OR IGNORE INTO", "INSERT INTO").replace("?", "%s")
        query = query.replace("expires REAL", "expires DOUBLE PRECISION")
        if ignore:
            query += " ON CONFLICT DO NOTHING"
        return self.connection.execute(query, parameters)

    def commit(self):
        pass  # Only the outer connection context may commit the whole operation.


@contextmanager
def connect():
    import psycopg
    if _active.get() is not None:
        yield _active.get()
        return
    if not settings.DATABASE_URL:
        raise HTTPException(503, "Persistent storage is not configured. Set server DATABASE_URL for STORAGE_BACKEND=postgres.")
    try:
        with psycopg.connect(settings.DATABASE_URL, row_factory=record_factory,
                             connect_timeout=10, prepare_threshold=None) as connection:
            connection.execute("SET LOCAL statement_timeout = '60s'")
            token = _active.set(Connection(connection))
            try:
                yield _active.get()
            finally:
                _active.reset(token)
    except psycopg.Error:
        # Driver messages may contain credentials, hosts or SQL/evidence values.
        raise HTTPException(503, "Persistent database is unavailable or the transaction failed. Retry after checking the server database configuration.") from None


def initialize():
    with connect() as conn:
        # Serialize additive DDL across simultaneous cold starts.
        conn.execute("SELECT pg_advisory_xact_lock(1871236401)")
        for statement in (
            "CREATE TABLE IF NOT EXISTS cases(case_id TEXT PRIMARY KEY,title TEXT NOT NULL,metadata TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text))",
            "CREATE TABLE IF NOT EXISTS documents(document_id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,filename TEXT NOT NULL,sha256 TEXT NOT NULL,content TEXT NOT NULL,storage_path TEXT,raw_content BYTEA,created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),UNIQUE(case_id,sha256))",
            "ALTER TABLE documents ADD COLUMN IF NOT EXISTS raw_content BYTEA",
            "CREATE TABLE IF NOT EXISTS entities(entity_id TEXT NOT NULL,case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,label TEXT NOT NULL,name TEXT NOT NULL,properties TEXT NOT NULL,PRIMARY KEY(case_id,entity_id))",
            "CREATE TABLE IF NOT EXISTS relationships(relationship_id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,source TEXT NOT NULL,target TEXT NOT NULL,rel_type TEXT NOT NULL,properties TEXT NOT NULL,evidence TEXT NOT NULL,certainty TEXT NOT NULL)",
            "CREATE INDEX IF NOT EXISTS idx_entities_case ON entities(case_id)",
            "CREATE INDEX IF NOT EXISTS idx_relationships_case ON relationships(case_id)",
            "CREATE TABLE IF NOT EXISTS officers(badge TEXT PRIMARY KEY,name_key TEXT UNIQUE NOT NULL,record TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS officer_sessions(token_hash TEXT PRIMARY KEY,badge TEXT NOT NULL,expires DOUBLE PRECISION NOT NULL)",
        ):
            conn.execute(statement)
