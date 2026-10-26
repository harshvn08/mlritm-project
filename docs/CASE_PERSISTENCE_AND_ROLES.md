# Case persistence and role corrections

The active React/FastAPI application in this repository produces the reported
`Case ... created (CASE_...)` notification. The separate checkout under
`C:\MLRITM_Crime_Branch` uses a different API and is not the source of that notification.

## Storage

Local SQLite uses an absolute database path shared by all routes, with transactions
committed before ingestion returns. `SQLITE_PATH` from settings or `.env` is now
honored, as is `ATLAS_SQLITE_PATH`. Put the database and `ATLAS_UPLOAD_DIR` on durable
storage when running a persistent backend service. Back up both together.

Vercel's `/tmp` is ephemeral and isolated between function instances. It cannot
provide persistent SQLite case storage: creation can succeed on one instance and
retrieval fail on another. SQLite ingestion on Vercel therefore returns an explicit
503. Use a persistent backend with SQLite or provision/configure the existing
Neo4j adapter. No external database was provisioned or production deployment changed.

Case detail retrieval now queries the primary key directly instead of searching
the first 500 list results. Case selection is retained in the URL across refreshes.
Browser snapshots cannot overwrite database records.

## Roles and reprocessing

Every SQLite person has normalized `roles`, `role`, `role_conflict`, and
`role_provenance` properties. Role provenance contains the source filename,
record/line, exact supporting text, and extraction method. Unspecified roles are
`unknown`; graph connectivity never assigns a suspect role. Conflicting explicit
roles are preserved and excluded from victim/suspect panels pending review.

The case detail, graph, dossier, and JSON report consume these same properties.
Reports are available at `GET /api/cases/{case_id}/report`.

Use **Reprocess Stored Evidence** on a case or
`POST /api/cases/{case_id}/reprocess` to correct previously stored derived analysis.
The operation reparses all preserved case documents and replaces only that case's
derived nodes and relationships inside a transaction. It preserves the case ID,
title, owner, and all uploaded source files. No mass migration or database reset
is performed. Missing source evidence cannot be reconstructed from old UI counts.

## Access

Login/signup returns an opaque `accessToken` in the session. Its SHA-256 hash and
24-hour expiry are stored in the additive `officer_sessions` table. API requests
send the bearer token. Owned SQLite cases require the matching officer, including
graph, file attachment, reprocessing, and report requests. Existing sessions without
a token must sign in again. Unowned historical/development cases remain unowned;
production API access requires login. This does not infer or reassign ownership
from browser-local case lists.

## Regression checks

`pytest -q tests/unit tests/integration` includes real SQLite API tests for eight
sequential uploads, canonical ID retrieval, a fresh application client, roles,
evidence inventory, report/graph agreement, role conflicts, missing-case 404,
other-officer denial, and direct retrieval beyond 500 cases.

Synthetic UI fixtures live in `tests/fixtures/synthetic_roles_*`. They contain
fictional test information only. The browser verification used a separate store
under `data/synthetic-ui-regression` and did not reset existing case data.

Verified on 2026-10-26: **245 unit/integration tests passed** and
`npm run build` succeeded (existing bundle-size warning). Browser ingestion created
`CASE_1793029636491`, then loaded it immediately and after reload. A second upload
left three documents in its inventory, with 7 extracted entities and 3 source
relationships. The rendered panels and downloaded JSON report agreed on all five
roles. The requested incident ID `CASE_1793028921825` is not present in the local
database; no access to that hosted deployment's database/logs was available.

## Changed source files

- `backend/local_store.py`: committed case lookup, inventory rebuild, role properties, metadata, reporting and access checks.
- `backend/storage_paths.py`: inspected existing Vercel scratch-storage behavior; unchanged.
- `backend/case_access.py`: opaque database-backed sessions and owner checks.
- `backend/main.py`: request authentication context and production login requirement.
- `backend/routers/auth.py`: session issuance after existing credential validation.
- `backend/routers/cases.py`: preserved-evidence reprocessing and JSON report routes.
- `backend/routers/ingest.py`: existing-case validation, authenticated ownership and serverless storage failure.
- `backend/services/person_roles.py`: explicit role extraction, normalization and provenance.
- `backend/services/ingestion_engine.py`: role-aware parsing and unknown defaults.
- `backend/services/schema_mapper.py`: unknown default for unclassified people.
- `backend/models/ingestion_models.py`: unknown default role.
- `backend/services/graph_service.py`: stop classifying untagged people as suspects.
- `backend/services/graph_writes.py`: serialize role provenance for Neo4j property compatibility.
- `frontend/src/App.tsx`: database authority and canonical case selection across reload.
- `frontend/src/services/api.ts`: authenticated API requests.
- `frontend/src/components/CaseRegistryModule.tsx`: role panels, actual document content, metadata, reprocessing and report export.
- `frontend/src/components/GraphExplorerModule.tsx`: authoritative role styling and legend.
- `tests/integration/test_case_role_regressions.py` and three `tests/fixtures/synthetic_roles_*` files: reproducible synthetic regressions.

Live Neo4j and the deployed Vercel service were not tested. No production database
was reset or migrated. The only additive table is `officer_sessions`; derived role
data uses the existing entity JSON properties. Reprocessing was exercised on the
isolated synthetic test cases, not automatically on unrelated existing cases.
