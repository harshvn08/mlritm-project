# Existing-case role extraction correction — 2026-10-26

Case: `CASE_1793030710494`, title `murder`, source identifier `MUR-2026-041`.

## Root cause from the actual stored evidence

- `FIR-MUR-2026-041.txt` explicitly labels only `Victim: Vikram Rao`.
  The other names occur in preliminary narrative leads, which do not assign roles.
- `witness_statements.txt` labels the other people in headings such as
  `STATEMENT W-001 — NEHA REDDY (WITNESS)`. The previous role parser only supported
  `Role: Name` and `Name — Role`, so it missed these statement headings.
- `evidence_person_mapping.csv` contains `entity_name`, `entity_type`, `relationship`,
  `related_entity`, and `record_reference`, with narrative verification statuses.
  Neither the generic CSV parser nor the explicit entity/relationship register
  adapter handled that schema. The file was stored, but its mapping rows were discarded.
- Arjun and Karan are explicitly labeled `PERSON OF INTEREST / SUSPECT A` and
  `PERSON OF INTEREST / SUSPECT B`. These compatible assertions must both be
  preserved, with suspect as the primary display role. This is distinct from a
  conflicting victim/suspect assignment, which still requires review.

## Fixes

- `backend/services/person_roles.py`: parse statement headings; retain dual role
  provenance; recognize the explicit `victim_in_case` mapping; use normalized
  primary roles and preserve true conflicts. Extraction version is now 2.
- `backend/services/evidence_person_mapping.py`: process person-mapping rows,
  including phone use, vehicle associations, statements, and person-to-person
  relations. Preserve mapping ID, source CSV row, referenced file/record, confidence,
  verification status and exact row text. Phone use never implies a suspect role.
- `backend/local_store.py`: recognize the mapping schema, apply it to the persisted
  graph, and return role panels using the authoritative primary role.
- `backend/services/ingestion_engine.py`: accept `caller_phone`, `receiver_phone`,
  and `timestamp_local`, which are the actual CDR headers. Preserve local timestamps
  in financial records too.
- `backend/main.py`: permit the HTML application shell on a case URL refresh;
  enforce ownership on data APIs, where the browser sends its bearer token.
- `tests/integration/test_role_heading_mapping.py` and fixtures under
  `tests/fixtures/role_heading_regression/`: regressions for these exact input
  formats, mapping-only uploads, role persistence and source preservation.
- `tests/integration/test_case_role_regressions.py`: authenticated case-page refresh
  continues to serve the shell while unauthenticated case data stays protected.

No frontend role names or fallback results were added. The existing frontend
already reads `victims`, `primary_suspects`, `witnesses`, and `persons_of_interest`.

## Existing data and verification

Before reprocessing, a SQLite backup was created at
`data/atlas_local.before-role-heading-fix-78ff10b4.sqlite3`.
Only this case's derived graph was rebuilt from its actual stored files. All
8 document IDs and SHA-256 hashes were verified unchanged; the other 7 case rows
were also verified unchanged. No case or source evidence was deleted.

The real API-backed report and refreshed browser show:

- Vikram Rao: primary `victim`.
- Arjun Mehta and Karan Shah: primary `suspect`, retaining both source labels
  `suspect` and `person_of_interest` in their role arrays.
- Neha Reddy: primary `witness`.
- Rohit Verma: primary `person_of_interest`.

The mapping file contributes 7 explicit graph links; `victim_in_case` is retained
as role/case metadata rather than a fabricated edge to an unstored case ID.
The CDR now contributes all 6 call records. All relationships retain source
references. The whole case contains 27 extracted entities and 22 relationships;
those counts include the existing extraction of additional identifiers, accounts,
and unclassified records, not just the five requested people.

Browser verification used the existing signed-in officer, the real backend on
port 8000 and the existing SQLite database. It checked all four panels after a
reload, downloaded the actual API report, and rendered the persisted person graph
with victim/suspect/witness/person-of-interest colors.
