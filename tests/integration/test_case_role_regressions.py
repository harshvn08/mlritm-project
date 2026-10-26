import json
from fastapi.testclient import TestClient
from backend.main import app
from backend import local_store
from backend.config import settings
from backend.services.auth_service import auth_service


def test_case_persistence_inventory_roles_and_access(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "sqlite")
    monkeypatch.setattr(local_store, "DB_PATH", tmp_path / "regression.sqlite3")
    monkeypatch.setattr(local_store, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(auth_service, "storage_path", tmp_path / "users.json")
    case_id = "CASE-SYNTHETIC-ROLE-REGRESSION"
    fir = """SYNTHETIC EVIDENCE ONLY
FIR Number: SYN/0042/2026
Crime Classification: SYNTHETIC THEFT
Vikram Rao — Victim
Arjun Mehta — Suspect A
Karan Shah — Suspect B
Neha Reddy — Witness
Rohit Verma — Person of interest
"""
    files = [
        ("fir.txt", fir, "text/plain"),
        ("cdr.csv", "Caller_MSISDN,Recipient_MSISDN,Timestamp\n1111111111,2222222222,2026-10-24T10:00:00", "text/csv"),
        ("financial.csv", "sender_account,receiver_account,amount\nSYN-001,SYN-002,100", "text/csv"),
        ("cctv.txt", "Location: Synthetic Market\nWitness: Neha Reddy", "text/plain"),
        ("vehicle.txt", "Victim: Vikram Rao; Phone: 1111111111", "text/plain"),
        ("forensic.txt", "Email: fictional@example.test", "text/plain"),
        ("witness.txt", "Witness: Neha Reddy", "text/plain"),
        ("mapping.csv", "person_name,role\nVikram Rao,victim\nArjun Mehta,suspect\nKaran Shah,suspect\nNeha Reddy,witness\nRohit Verma,person_of_interest", "text/csv"),
    ]
    with TestClient(app) as client:
        for badge in ("SYN-OFFICER-A", "SYN-OFFICER-B"):
            response = client.post("/api/auth/signup", json={"badgeNumber": badge, "officerName": badge, "password": "synthetic-password"})
            assert response.status_code == 201, response.text
            if badge.endswith("A"): owner_token = response.json()["session"]["accessToken"]
            else: other_token = response.json()["session"]["accessToken"]
        headers = {"Authorization": f"Bearer {owner_token}"}
        for index, (filename, text, mime) in enumerate(files, 1):
            result = client.post(f"/api/v1/ingest?case_id={case_id}&existing_case={'false' if index == 1 else 'true'}", headers=headers,
                                 data={"case_title": "Synthetic role regression"} if index == 1 else {},
                                 files=[("files", (filename, text.encode(), mime))])
            assert result.status_code == 201, result.text
            assert result.json()["case_id"] == case_id
            detail = client.get(f"/api/cases/{case_id}", headers=headers)
            assert detail.status_code == 200, detail.text
            assert detail.json()["case_id"] == case_id
            assert len(detail.json()["documents"]) == index
        case = detail.json()
        assert case["case_name"] == "Synthetic role regression"
        assert case["fir_number"] == "SYN/0042/2026"
        assert case["case_type"] == "SYNTHETIC THEFT"
        expected = {"Vikram Rao": "victim", "Arjun Mehta": "suspect", "Karan Shah": "suspect", "Neha Reddy": "witness", "Rohit Verma": "person_of_interest"}
        graph = client.get("/api/graph", params={"case_id": case_id}, headers=headers).json()
        for name, role in expected.items():
            person = next(n for n in graph["nodes"] if n["name"] == name)
            assert person["properties"]["roles"] == [role], person
            assert person["properties"]["role_provenance"]
        assert [p["name"] for p in case["victims"]] == ["Vikram Rao"]
        assert {p["name"] for p in case["primary_suspects"]} == {"Arjun Mehta", "Karan Shah"}
        assert graph["edges"]
        assert all(e["evidence"] for e in graph["edges"])
        assert client.get(f"/api/cases/{case_id}/report", headers=headers).json()["case"]["victims"] == case["victims"]
        assert client.get(f"/api/cases/{case_id}", headers={"Authorization": f"Bearer {other_token}"}).status_code == 403
        assert client.get("/api/graph", params={"case_id": case_id}, headers={"Authorization": f"Bearer {other_token}"}).status_code == 403
        assert client.get(f"/api/cases/{case_id}/report", headers={"Authorization": f"Bearer {other_token}"}).status_code == 403
        assert not client.get("/api/graph", headers={"Authorization": f"Bearer {other_token}"}).json()["nodes"]
        assert client.get(f"/api/cases/{case_id}").status_code == 403
        assert client.get(f"/?case_id={case_id}").status_code == 200
        assert client.get("/api/cases/DOES-NOT-EXIST", headers=headers).status_code == 404
        assert client.post("/api/v1/ingest?case_id=DOES-NOT-EXIST&existing_case=true", headers=headers, files=[("files", ("fir.txt", fir.encode(), "text/plain"))]).status_code == 404
        assert client.post(f"/api/cases/{case_id}/reprocess", headers=headers).status_code == 200
    # A fresh application client reads the same committed database and stored login.
    with TestClient(app) as client:
        persisted = client.get(f"/api/cases/{case_id}", headers=headers).json()
        assert len(persisted["documents"]) == 8
        assert persisted["victims"][0]["name"] == "Vikram Rao"
        conflict = client.post(f"/api/v1/ingest?case_id={case_id}&existing_case=true", headers=headers,
                              files=[("files", ("conflict.txt", b"Suspect: Vikram Rao", "text/plain"))])
        assert conflict.status_code == 201, conflict.text
        updated = client.get(f"/api/cases/{case_id}", headers=headers).json()
        assert updated["role_conflicts"][0]["name"] == "Vikram Rao"
        assert not any(p["name"] == "Vikram Rao" for p in updated["primary_suspects"])


def test_detail_uses_primary_key_not_list_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(local_store, "DB_PATH", tmp_path / "lookup.sqlite3")
    monkeypatch.setattr(local_store, "UPLOAD_DIR", tmp_path / "uploads")
    local_store.initialize()
    with local_store.connect() as conn:
        for index in range(505):
            conn.execute("INSERT INTO cases(case_id,title,metadata,created_at) VALUES(?,?,?,?)", (f"CASE-{index}", f"Case {index}", json.dumps({"case_id": f"CASE-{index}"}), f"{index:08d}"))
    assert local_store.case_detail("CASE-0")["case_id"] == "CASE-0"


def test_ephemeral_serverless_sqlite_does_not_claim_case_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "sqlite")
    monkeypatch.setattr(local_store, "DB_PATH", tmp_path / "ephemeral.sqlite3")
    monkeypatch.setattr(local_store, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setenv("VERCEL", "1")
    with TestClient(app) as client:
        response = client.post("/api/v1/ingest?case_id=CASE-EPHEMERAL", files=[("files", ("fir.txt", b"Victim: Fictional Example", "text/plain"))])
        assert response.status_code == 503, response.text
        assert "persistent shared storage" in response.json()["detail"]
        assert local_store.case_detail("CASE-EPHEMERAL") is None
