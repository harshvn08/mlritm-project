from pathlib import Path
from fastapi.testclient import TestClient
from backend import local_store
from backend.config import settings
from backend.main import app
from backend.services.person_roles import extract_roles, role_properties

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "role_heading_regression"


def test_statement_heading_roles_and_compatible_labels():
    assertions = extract_roles([("witness_statements.txt", (FIXTURES / "witness_statements.txt").read_text(encoding="utf-8"))])
    expected = {"Neha Reddy": "witness", "Arjun Mehta": "suspect", "Karan Shah": "suspect", "Rohit Verma": "person_of_interest"}
    for name, primary_role in expected.items():
        result = role_properties([a for a in assertions if a[0] == name])
        assert result["role"] == primary_role
        assert not result["role_conflict"]
        assert result["role_provenance"][0]["source_document"] == "witness_statements.txt"
    assert role_properties([("Example Person", role, "source.txt", 1, "explicit") for role in ("victim", "suspect")])["role_conflict"]


def test_mapping_csv_is_processed_and_reprocessing_retains_roles_and_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "sqlite")
    monkeypatch.setattr(local_store, "DB_PATH", tmp_path / "roles.sqlite3")
    monkeypatch.setattr(local_store, "UPLOAD_DIR", tmp_path / "uploads")
    case_id = "CASE-HEADING-MAPPING-REGRESSION"
    with TestClient(app) as client:
        # A mapping-only upload must succeed; generic CSV fallback previously
        # discarded these columns and reported no extractable entities.
        result = client.post(f"/api/v1/ingest?case_id={case_id}", data={"case_title": "Source heading regression"},
                             files=[("files", ("evidence_person_mapping.csv", (FIXTURES / "evidence_person_mapping.csv").read_bytes(), "text/csv"))])
        assert result.status_code == 201, result.text
        graph = client.get("/api/graph", params={"case_id": case_id}).json()
        assert len([e for e in graph["edges"] if e["properties"].get("mapping_id")]) == 7
        people = [n for n in graph["nodes"] if "Person" in n["labels"]]
        # A phone association alone must not classify a person as a suspect.
        assert next(n for n in people if n["name"] == "Arjun Mehta")["properties"]["role"] == "unknown"
        for name in ("witness_statements.txt", "FIR-MUR-2026-041.txt"):
            upload = client.post(f"/api/v1/ingest?case_id={case_id}&existing_case=true",
                                 files=[("files", (name, (FIXTURES / name).read_bytes(), "text/plain"))])
            assert upload.status_code == 201, upload.text
        detail = client.get(f"/api/cases/{case_id}").json()
        assert {p["name"] for p in detail["victims"]} == {"Vikram Rao"}
        assert {p["name"] for p in detail["primary_suspects"]} == {"Arjun Mehta", "Karan Shah"}
        assert {p["name"] for p in detail["witnesses"]} == {"Neha Reddy"}
        assert {p["name"] for p in detail["persons_of_interest"]} == {"Rohit Verma"}
        assert not detail["role_conflicts"]
        before = {(d["document_id"], d["sha256"]) for d in detail["documents"]}
        assert client.post(f"/api/cases/{case_id}/reprocess").status_code == 200
    with TestClient(app) as client:
        detail = client.get(f"/api/cases/{case_id}").json()
        assert {(d["document_id"], d["sha256"]) for d in detail["documents"]} == before
        assert len(detail["people"]) == 5
        graph = client.get("/api/graph", params={"case_id": case_id}).json()
        assert {p["name"]: p["role"] for p in detail["people"]} == {n["name"]: n["properties"]["role"] for n in graph["nodes"] if "Person" in n["labels"]}
        for edge in graph["edges"]:
            if edge["properties"].get("mapping_id"):
                assert edge["properties"]["record_reference"]
                assert edge["properties"]["source_register"] == "evidence_person_mapping.csv"
                assert edge["properties"]["evidence_text"]
        arjun = next(p for p in detail["people"] if p["name"] == "Arjun Mehta")
        assert set(arjun["roles"]) == {"suspect", "person_of_interest"}
        assert any(p["source_document"] == "witness_statements.txt" and "SUSPECT A" in p["evidence_text"] for p in arjun["role_provenance"])
