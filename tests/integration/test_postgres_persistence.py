"""Real PostgreSQL regression suite. Use an isolated TEST_DATABASE_URL only."""
import os
import json
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from backend.main import app
from backend.config import settings
from backend import local_store
from backend.case_access import current_officer


@pytest.fixture
def postgres(monkeypatch, tmp_path):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to an isolated PostgreSQL database")
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "postgres")
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setattr(local_store, "UPLOAD_DIR", tmp_path / "must-not-exist")
    local_store.initialize()
    with TestClient(app) as client:
        badge = "PG-" + uuid.uuid4().hex
        response = client.post("/api/auth/signup", json={"badgeNumber": badge, "officerName": badge, "password": "synthetic-test-password"})
        assert response.status_code == 201, response.text
        headers = {"Authorization": "Bearer " + response.json()["session"]["accessToken"]}
        yield client, headers, badge


def upload(client, headers, cid, name, raw, existing=False):
    return client.post("/api/v1/ingest", params={"case_id": cid, "existing_case": str(existing).lower()},
        headers=headers, data={"case_title": "Synthetic persistent evidence test"}, files=[("files", (name, raw, "text/plain"))])


def test_real_database_ingestion_roles_graph_and_fresh_process(postgres):
    import subprocess
    import sys
    client, headers, badge = postgres
    cid = "PG-" + uuid.uuid4().hex
    fir = Path("tests/fixtures/role_heading_regression/FIR-MUR-2026-041.txt").read_bytes()
    assert upload(client, headers, cid, "FIR-MUR-2026-041.txt", fir).status_code == 201
    for filename in ("witness_statements.txt", "evidence_person_mapping.csv"):
        raw = Path("tests/fixtures/role_heading_regression", filename).read_bytes()
        response = upload(client, headers, cid, filename, raw, True)
        assert response.status_code == 201, response.text
    case = client.get(f"/api/cases/{cid}", headers=headers).json()
    roles = {p["name"]: p["roles"] for p in case["people"]}
    for name, role in {"Vikram Rao":"victim", "Arjun Mehta":"suspect", "Karan Shah":"suspect", "Neha Reddy":"witness", "Rohit Verma":"person_of_interest"}.items():
        assert role in roles[name]
    assert len(case["documents"]) == 3
    graph = client.get("/api/graph", params={"case_id":cid}, headers=headers).json()
    assert graph["edges"]
    assert all(edge["evidence"] for edge in graph["edges"])
    assert client.get(f"/api/cases/{cid}/report", headers=headers).json()["case"]["witnesses"]
    assert not local_store.UPLOAD_DIR.exists()  # No durable evidence relies on local files.
    with local_store.connect() as conn:
        original = conn.execute("SELECT raw_content FROM documents WHERE case_id=? AND filename=?", (cid,"FIR-MUR-2026-041.txt")).fetchone()[0]
        assert bytes(original) == fir
    # Entirely new Python process: no application cache, no prior DB connection.
    env = {**os.environ, "STORAGE_BACKEND":"postgres", "DATABASE_URL":settings.DATABASE_URL, "APP_ENV":"production"}
    script = """import json,sys
from fastapi.testclient import TestClient
from backend.main import app
with TestClient(app) as client:
 r=client.get('/api/cases/'+sys.argv[1],headers=json.loads(sys.argv[2]))
 assert r.status_code==200,r.text
 c=r.json();assert len(c['documents'])==3;assert c['victims'][0]['name']=='Vikram Rao'
 print('fresh-process retrieval passed')
"""
    result = subprocess.run([sys.executable,"-c",script,cid,json.dumps(headers)], env=env,capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
    assert client.post(f"/api/cases/{cid}/reprocess", headers=headers).status_code == 200
    assert len(client.get(f"/api/cases/{cid}",headers=headers).json()["documents"]) == 3
    assert client.get("/api/cases/PG-NONEXISTENT",headers=headers).status_code == 404
    assert client.get(f"/api/cases/{cid}").status_code == 401
    from backend.case_access import issue_session
    other = issue_session("PG-OTHER")
    assert client.get(f"/api/cases/{cid}",headers={"Authorization":"Bearer "+other}).status_code == 403


def test_concurrent_upload_inventory_is_not_lost(postgres):
    client, headers, badge = postgres
    cid = "PG-" + uuid.uuid4().hex
    assert upload(client,headers,cid,"fir.txt",b"Victim: Avery Gray").status_code == 201
    def add(index):
        with TestClient(app) as other:
            return upload(other,headers,cid,f"witness-{index}.txt",f"Witness: Mira Vale\nReference {index}".encode(),True).status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(add,range(2))) == [201,201]
    assert len(client.get(f"/api/cases/{cid}",headers=headers).json()["documents"]) == 3


def test_transaction_failure_does_not_replace_saved_evidence(postgres, monkeypatch):
    client, headers, badge = postgres
    cid = "PG-" + uuid.uuid4().hex
    assert upload(client,headers,cid,"fir.txt",b"Victim: Avery Gray").status_code == 201
    from backend.postgres_store import Connection
    execute = Connection.execute
    def fail(self, query, parameters=()):
        if query.startswith("INSERT INTO entities"):
            return execute(self,"SELECT 1/0")
        return execute(self,query,parameters)
    monkeypatch.setattr(Connection,"execute",fail)
    response = upload(client,headers,cid,"witness.txt",b"Witness: Mira Vale",True)
    assert response.status_code == 503, response.text
    detail = client.get(f"/api/cases/{cid}",headers=headers).json()
    assert len(detail["documents"]) == 1
    assert detail["victims"][0]["name"] == "Avery Gray"


def test_unavailable_database_fails_without_sqlite_fallback(postgres, monkeypatch):
    client,headers,_ = postgres
    monkeypatch.setattr(settings,"DATABASE_URL","postgresql://synthetic@127.0.0.1:1/no_database?connect_timeout=1")
    response = client.get("/api/cases",headers=headers)
    assert response.status_code == 503
    assert upload(client,headers,"UNAVAILABLE","fir.txt",b"Victim: Avery Gray").status_code == 503
    assert "synthetic@" not in response.text
    assert client.get("/api/health").status_code == 503
    assert not local_store.UPLOAD_DIR.exists()


@pytest.mark.parametrize("endpoint",["/api/v1/ingest/case","/api/cases/ingest"])
def test_json_ingestion_uses_selected_database(postgres,endpoint):
    client,headers,_ = postgres
    cid = "JSON-"+uuid.uuid4().hex
    payload = {"case_metadata":{"case_id":cid,"case_name":"Synthetic JSON evidence"},
               "entities":{"people":[{"person_id":"SYN-PERSON","name":"Avery Gray","roles":["victim"]}]}}
    response = client.post(endpoint,headers=headers,json=payload)
    assert response.status_code == 201, response.text
    case = client.get(f"/api/cases/{cid}",headers=headers).json()
    assert case["victims"][0]["name"] == "Avery Gray"
    assert len(case["documents"]) == 1


def test_text_ingestion_uses_selected_database(postgres):
    client,headers,_ = postgres
    response = client.post("/api/v1/ingest/text",headers={**headers,"Content-Type":"text/plain"},content="Victim: Avery Gray\nWitness: Mira Vale")
    assert response.status_code == 201, response.text
    cid = response.json()["case_id"]
    case = client.get(f"/api/cases/{cid}",headers=headers).json()
    assert case["victims"][0]["name"] == "Avery Gray"
    assert case["witnesses"][0]["name"] == "Mira Vale"
    second = client.post("/api/v1/ingest/text",headers={**headers,"Content-Type":"text/plain"},content="Victim: Avery Gray\nWitness: Mira Vale")
    assert second.status_code == 201
    assert second.json()["case_id"] != cid  # No shared CASE_001 collision for new cases.


def test_missing_database_url_does_not_fall_back(monkeypatch):
    monkeypatch.setattr(settings,"STORAGE_BACKEND","postgres")
    monkeypatch.setattr(settings,"DATABASE_URL",None)
    with pytest.raises(Exception) as exc:
        with local_store.connect(): pass
    assert exc.value.status_code == 503


def test_import_preserves_bytes_roles_and_source_and_refuses_conflicts(postgres, monkeypatch, tmp_path):
    import hashlib
    from backend.models.case_input import CaseData, CaseMetadata
    from backend.migrate_sqlite_to_postgres import migrate
    _,_,badge = postgres
    source = tmp_path / "original.sqlite3"
    monkeypatch.setattr(local_store,"DB_PATH",source)
    monkeypatch.setattr(local_store,"UPLOAD_DIR",tmp_path / "evidence")
    monkeypatch.setattr(settings,"STORAGE_BACKEND","sqlite")
    cid = "MIGRATION-"+uuid.uuid4().hex
    text = "Victim: Avery Gray\nWitness: Mira Vale"
    raw = b"\xef\xbb\xbf"+text.encode()
    local_store.ingest(cid,"Migration synthetic",CaseData(case_metadata=CaseMetadata(case_id=cid,case_name="Migration synthetic")),[("fir.txt",raw,text)],badge)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(settings,"STORAGE_BACKEND","postgres")
    assert migrate(source)["applied"] is False
    with local_store.connect() as conn:
        assert conn.execute("SELECT * FROM cases WHERE case_id=?",(cid,)).fetchone() is None
    assert migrate(source,apply=True)["records"]["documents"] == 1
    assert migrate(source,apply=True)["source_preserved"]
    with local_store.connect() as conn:
        doc = conn.execute("SELECT raw_content FROM documents WHERE case_id=?",(cid,)).fetchone()
        assert bytes(doc[0]) == raw
        conn.execute("UPDATE cases SET title=? WHERE case_id=?",("Newer database title",cid))
    with pytest.raises(ValueError,match="conflict"):
        migrate(source,apply=True)
    with local_store.connect() as conn:
        assert conn.execute("SELECT title FROM cases WHERE case_id=?",(cid,)).fetchone()[0] == "Newer database title"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_import_missing_original_evidence_aborts_without_creating_case(postgres, monkeypatch, tmp_path):
    from backend.models.case_input import CaseData, CaseMetadata
    from backend.migrate_sqlite_to_postgres import migrate
    _,_,badge = postgres
    source = tmp_path / "original.sqlite3"
    monkeypatch.setattr(local_store,"DB_PATH",source)
    monkeypatch.setattr(local_store,"UPLOAD_DIR",tmp_path / "evidence")
    monkeypatch.setattr(settings,"STORAGE_BACKEND","sqlite")
    cid = "MISSING-"+uuid.uuid4().hex
    text = "Victim: Avery Gray"
    local_store.ingest(cid,"Missing file test",CaseData(case_metadata=CaseMetadata(case_id=cid,case_name="Missing file test")),[("fir.txt",text.encode(),text)],badge)
    with local_store.connect() as conn:
        path = conn.execute("SELECT storage_path FROM documents WHERE case_id=?",(cid,)).fetchone()[0]
    Path(path).unlink()  # Synthetic fixture only, never real uploaded evidence.
    monkeypatch.setattr(settings,"STORAGE_BACKEND","postgres")
    with pytest.raises(ValueError,match="Original evidence missing"):
        migrate(source,apply=True)
    with local_store.connect() as conn:
        assert conn.execute("SELECT * FROM cases WHERE case_id=?",(cid,)).fetchone() is None
