# Persistent evidence storage on Vercel

## Cause and architecture

Vercel Services routes `/api/*` to FastAPI and the remaining paths to Vite.
Neither service has a persistent application disk. SQLite defaults to `/tmp`,
so a later instance can have a different database. The ingestion 503 guard is
intentional and remains in place for Vercel + SQLite.

Use `STORAGE_BACKEND=postgres` with a shared external PostgreSQL database for
this application's current UI. The existing Neo4j path stores graph data, but
does not implement the full original-file inventory, stored-file reprocessing,
role panels and reports supported by the relational evidence pipeline. Switching
only to Neo4j would also leave officer credentials and sessions on ephemeral disk.
PostgreSQL reuses that pipeline with minimal schema changes and keeps these
records together. Neo4j remains available for its existing endpoints; there is
no automatic switch or fallback between backends.

The application stores case metadata, normalized roles and provenance, derived
graph nodes/relationships, extracted document text, checksums, and **original
file bytes** in PostgreSQL. Officers and hashed opaque login tokens use the same
database. No evidence or credential record relies on a local file in this mode.
Graph analysis and report output continue to use the existing code. The optional
blockchain demo ledger remains a separate file and is not used as evidence storage.

Writes commit before ingestion success. A transaction-scoped advisory lock
serializes the complete read/merge/write operation for each case. Failed writes
roll back metadata, documents and derived graph data together. Connections close
after each operation. Automatic prepared statements are disabled for transaction
pooler compatibility. Schema initialization uses additive CREATE/ALTER operations
under a transaction lock; no tables are dropped or truncated.

## Manual production setup

1. Use an existing external PostgreSQL database or connect a PostgreSQL resource
   from Vercel Marketplace. No resource, paid plan or provider change is provisioned
   by this patch. Choose a region near the API and enable backups with the provider.
2. Obtain the provider's **pooled PostgreSQL connection URL** with TLS enabled
   (normally `sslmode=require`, or the provider's stronger verification settings).
   Keep the URL private: it contains database credentials.
3. In **Vercel → atlas-criminal-network → Settings → Environment Variables**, set:
   - `STORAGE_BACKEND=postgres`
   - `DATABASE_URL`: the complete private pooled URL
   - `APP_ENV=production`
   - `GEMINI_API_KEY`: separate server secret, only needed for Gemini Copilot.
   Optional `GEMINI_MODEL=gemini-flash-latest`; no Neo4j variables are required
   for the PostgreSQL evidence backend. Do not use `VITE_` prefixes.
4. Select Production and the intended Preview environment. Use a separate database
   for Preview when it should not share production evidence.
5. Redeploy the new commit. `vercel.json` needs no credential or routing changes.
   The database account needs schema CREATE/ALTER and normal data privileges for
   initial setup. `/api/health` must return 200 with `database: postgres`.
6. Import preserved local cases and officers as below, or register a new officer.
   Sign in again: old instance-local tokens are not migrated.
7. Upload a clearly named synthetic FIR plus mapping/CDR files. Capture its exact
   returned case ID. Retrieve `/api/cases/{id}`, `/api/graph?case_id={id}` and
   `/api/cases/{id}/report` with the bearer token. Upload a second file to that same
   ID, refresh, then verify again from a new session/instance. Confirm inventory,
   roles, provenance, graph relationships, and owner-only access.

Source references: [Vercel storage integrations](https://vercel.com/docs/marketplace-storage),
[Psycopg connection and transaction usage](https://www.psycopg.org/psycopg3/docs/basic/usage.html).

## Preserve and migrate existing SQLite cases

Do not reset, delete or replace the existing `data/` directory. Back up the SQLite
database with SQLite's backup API (rather than copying an active WAL database),
the entire uploads directory, and `users.json`. Retain the original file paths
while importing: documents record those paths. A complete backup of originals
is required; extracted text cannot reconstruct an original PDF/DOCX/XLSX file.

On a trusted machine with access to those originals, set the **server environment**
`STORAGE_BACKEND=postgres` and `DATABASE_URL` to the target. Do not put a URL on
the command line, in screenshots, or in Git. From the project root run:

```powershell
python -m backend.migrate_sqlite_to_postgres --source data/atlas_local.sqlite3 --users data/users.json
python -m backend.migrate_sqlite_to_postgres --source data/atlas_local.sqlite3 --users data/users.json --apply
```

The first command validates and rolls back its data writes (additive schema setup
is retained). The second imports all source cases, documents, entities,
relationships and optional officer password hashes in one transaction. Original
bytes must exist and match each checksum. It never re-extracts or overwrites source
or target records. Identical imports are idempotent; any differing target key
aborts the transaction for analyst review. Legacy unowned cases retain their
existing access semantics; review ownership before making them available to users.
No production import has been performed without a configured target.

## Verification

`TEST_DATABASE_URL` must point only to a dedicated regression database. Tests create
synthetic records there. No real credentials are required for SQLite tests.

```powershell
python -m pytest -q tests/unit tests/integration
cd frontend
npm run build
```

The PostgreSQL suite covers actual multipart ingestion, all five source roles,
relationships/provenance, original bytes, a fresh Python/API process using the
same saved token, second and concurrent uploads, reports/reprocessing,
unauthorized access, missing/unavailable databases, transaction rollback, and
non-destructive migration/conflict/missing-original checks. Without
`TEST_DATABASE_URL`, database-dependent tests skip explicitly, never mock success.

Production acceptance remains pending until an actual synthetic upload and its
subsequent retrieval succeed against the configured production database.

### Actual results on 2026-10-26

- `python -m pytest -q tests/unit tests/integration` with an isolated real
  PostgreSQL 16 database: **280 passed**, 20 existing deprecation warnings.
- The final raw-text duplicate-case regression was also run separately: **1 passed**.
- `npm run build` in `frontend`: TypeScript and Vite passed; existing large-bundle
  warning remains. No frontend redesign or source changes were needed.
- A PostgreSQL server stop/start followed by a new Python reader recovered the
  synthetic case's **3 files, 10 entities, 7 relationships**, two suspects, victim,
  witness and person of interest.
- Migration was tested only on synthetic evidence; actual existing SQLite cases,
  original uploads and officer records were not changed or removed.
- Production health reported `database: sqlite` before publication. No hosted
  `DATABASE_URL` is configured locally. Production upload/retrieval acceptance is
  therefore **pending external database configuration**, not claimed successful.

### Files changed

- `.env.example`, `requirements.txt`, `VERCEL_DEPLOYMENT.md`
- `backend/config.py`, `backend/local_store.py`, `backend/main.py`
- `backend/postgres_store.py`, `backend/migrate_sqlite_to_postgres.py`
- `backend/routers/auth.py`, `backend/routers/cases.py`, `backend/routers/graph.py`
- `backend/routers/health.py`, `backend/routers/ingest.py`, `backend/routers/insights.py`
- `backend/routers/path.py`, `backend/routers/rankings.py`
- `backend/services/auth_service.py`, `backend/services/copilot_context.py`
- `tests/integration/test_postgres_persistence.py`, `docs/PERSISTENT_STORAGE.md`
