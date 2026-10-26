import json
from fastapi.testclient import TestClient
from backend.config import settings
from backend.main import app
from backend import local_store
from backend.services.ingestion_engine import CaseIngestionEngine, is_valid_person_name


def test_local_sqlite_upload_persists_case_graph_and_files(tmp_path, monkeypatch):
    monkeypatch.setattr(local_store, "DB_PATH", tmp_path / "atlas.sqlite3")
    monkeypatch.setattr(local_store, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "sqlite")
    csv_data = b"Caller_MSISDN,Recipient_MSISDN,Timestamp,Duration_Sec\n1111111111,2222222222,2026-10-24T10:00:00,45\n"
    narrative = b"Synthetic test record only.\nSuspect: Arjun Mehta; Mobile: 1111111111\nSuspect: Petra Rao; Mobile: 2222222222\nContact: analyst@example.test. Location: Delhi. Organization: Northwind Research."
    with TestClient(app) as client:
        assert client.get("/api/health").json()["database"] == "sqlite"
        response = client.post("/api/v1/ingest?case_id=CASE-LOCAL-TEST", data={"case_title": "Synthetic local workflow"}, files=[
            ("files", ("synthetic-cdr.csv", csv_data, "text/csv")),
            ("files", ("synthetic-note.txt", narrative, "text/plain")),
        ])
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["created"]["source_records"] == 2
        assert client.get("/api/cases").json()[0]["case_id"] == "CASE-LOCAL-TEST"
        graph = client.get("/api/graph", params={"case_id": "CASE-LOCAL-TEST"}).json()
        assert graph["total_nodes"] >= 4
        assert any(edge["type"] == "CALLED" for edge in graph["edges"])
        assert any(node["labels"] == ["Email"] for node in graph["nodes"])
        assert any(node["labels"] == ["Person"] and node["name"] == "Arjun Mehta" for node in graph["nodes"])
        assert any(node["labels"] == ["Person"] and node["name"] == "Petra Rao" for node in graph["nodes"])
        assert any(node["labels"] == ["Location"] and node["name"] == "Delhi" for node in graph["nodes"])
        assert any(node["labels"] == ["Organization"] and node["name"] == "Northwind Research" for node in graph["nodes"])
        assert not any(node["labels"] == ["SocialHandle"] and node["name"] == "@example" for node in graph["nodes"])
        found = client.get("/api/entities/search", params={"q": "analyst@example.test", "case_id": "CASE-LOCAL-TEST"}).json()
        assert found
        edge = next(edge for edge in graph["edges"] if edge["type"] == "CALLED")
        assert client.get(f"/api/relationships/{edge['id']}").json()["certainty"] == "observed"
        path = client.get("/api/cases/shortest-path", params={"suspect_name":"Arjun Mehta", "victim_name":"Petra Rao"})
        assert path.status_code == 200 and path.json()["path_length"] == 3, path.text
        # The deterministic utility remains independently testable; authenticated
        # Gemini chat is covered by test_gemini_copilot.py with SDK network mocks.
        main_suspect = local_store.copilot_answer("Who is the main suspect?", "CASE-LOCAL-TEST")
        assert main_suspect["intent"] == "suspect_safety" and "cannot establish" in main_suspect["answer"].lower()
        ranking = local_store.copilot_answer("Which entities are most connected?", "CASE-LOCAL-TEST")
        assert ranking["intent"] == "degree_ranking" and "1111111111" in ranking["answer"]
        missing = local_store.copilot_answer("What information is missing?", "CASE-LOCAL-TEST")
        assert missing["intent"] == "coverage" and "source documents" in missing["answer"]
        evidence = local_store.copilot_answer("Which evidence documents mention Arjun Mehta?", "CASE-LOCAL-TEST")
        assert "synthetic-note.txt" in evidence["answer"]
        no_case = local_store.copilot_answer("summarize this case", None)
        assert no_case["intent"] == "case_required"
        assert client.post("/api/v1/ingest?case_id=CASE-LOCAL-TEST", data={"case_title": "Synthetic local workflow"}, files=[
            ("files", ("bad.exe", b"nope", "application/octet-stream")),
        ]).status_code == 415
    # New connection sees persisted rows (restart persistence behavior).
    assert local_store.case_detail("CASE-LOCAL-TEST")["total_entities"] >= 3
    with local_store.connect() as conn:
        stored = conn.execute("SELECT storage_path FROM documents WHERE case_id=?", ("CASE-LOCAL-TEST",)).fetchall()
        assert len(stored) == 2 and all(__import__("pathlib").Path(row["storage_path"]).exists() for row in stored)


def test_structured_atlas_registers_create_typed_evidence_graph_and_copilot_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(local_store, "DB_PATH", tmp_path / "atlas.sqlite3")
    monkeypatch.setattr(local_store, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "sqlite")
    case_id = "CASE-AT-2026-041-STRUCTURED"
    local_store.initialize()
    # A pre-existing unrelated case must survive re-ingestion of the synthetic pack.
    with local_store.connect() as conn:
        conn.execute("INSERT INTO cases(case_id,title,metadata) VALUES(?,?,?)", ("CASE-KEEP", "Existing case", "{}"))
        conn.execute("INSERT INTO entities(entity_id,case_id,label,name,properties) VALUES(?,?,?,?,?)", ("KEEP-P1", "CASE-KEEP", "Person", "Existing Person", "{}"))

    entity_register = b"entity_id,entity_name,entity_type,identifier,notes\nP-001,Arjun Mehta,PERSON,PERSON-001,Driver listed in dispatch record; fictional\nP-002,Nisha Rao,PERSON,PERSON-002,Witness; fictional\nP-003,Dev Patel,PERSON,PERSON-003,Supervisor; fictional\nO-002,Northstar Components Ltd.,ORGANIZATION,ORG-002,Shipment sender; fictional\nL-001,Meridian Freight Depot,LOCATION,LOC-001,Depot; fictional\nV-001,Vehicle V-204,VEHICLE,V-204,Vehicle; fictional\nS-001,Shipment SH-771,SHIPMENT,SH-771,Shipment; fictional\nD-001,Dispatch Record DR-8841,DOCUMENT,DR-8841,Dispatch record\nD-002,Gate Log GL-219,DOCUMENT,GL-219,Gate log\nD-003,Delivery Instruction DI-551,DOCUMENT,DI-551,Instruction\nU-001,operator-17,USER_ACCOUNT,operator-17,Identity not established\n"
    relationship_register = b"relationship_id,source,target,type,evidence,status,note\nREL-001,Arjun Mehta,Vehicle V-204,ASSIGNED_TO,DR-8841,RECORDED,Driver listed against vehicle\nREL-002,Vehicle V-204,Meridian Freight Depot,ENTERED_LOCATION,GL-219,WITNESS_REPORTED,Witness reported vehicle entry\nREL-005,Northstar Components Ltd.,Shipment SH-771,SENDER_OF,DR-8841,RECORDED,Sender listed in dispatch record\nREL-008,operator-17,Delivery Instruction DI-551,AMENDED_BY_ACCOUNT,AM-052,RECORDED,Account field in audit export; identity unknown\nREL-009,Arjun Mehta,Dev Patel,CONTACT_EVENT,COM-201,RECORDED,Synthetic call record; content unavailable\n"
    dispatch = b"Synthetic AT-2026-041. DR-8841 lists Arjun Mehta as driver of Vehicle V-204. Northstar Components Ltd. sent Shipment SH-771."
    witness = b"Synthetic witness report WS-117: Nisha Rao reported seeing Vehicle V-204 enter Meridian Freight Depot; gate entry GL-219. This is witness-reported, not conclusive."
    audit = b"Synthetic audit memo AM-052: operator-17 amended Delivery Instruction DI-551. The real-world user identity is not established."
    communications = b"record_id,date_time,from_entity,to_entity,channel,duration_seconds\nCOM-201,2026-10-14 20:31,Arjun Mehta,Dev Patel,phone,84\n"
    with TestClient(app) as client:
        upload = client.post(f"/api/v1/ingest?case_id={case_id}", data={"case_title": "Synthetic AT-2026-041 Register Test"}, files=[
            ("files", ("06_Entity_Register.csv", entity_register, "text/csv")),
            ("files", ("07_Relationship_Register.csv", relationship_register, "text/csv")),
            ("files", ("dispatch.txt", dispatch, "text/plain")),
            ("files", ("witness.txt", witness, "text/plain")),
            ("files", ("audit.txt", audit, "text/plain")),
            ("files", ("05_Communication_Records.csv", communications, "text/csv")),
        ])
        assert upload.status_code == 201, upload.text
        graph = client.get("/api/graph", params={"case_id": case_id, "limit": 500}).json()
        nodes = {node["id"]: node for node in graph["nodes"]}
        edges = graph["edges"]
        assert graph["total_edges"] == 5
        assert {"P-001", "P-002", "P-003"}.issubset(nodes)
        assert nodes["P-001"]["entity_type"] == "PERSON"
        assert nodes["O-002"]["entity_type"] == "ORGANIZATION"
        assert nodes["V-001"]["entity_type"] == "VEHICLE"
        assert nodes["S-001"]["entity_type"] == "SHIPMENT"
        assert nodes["U-001"]["entity_type"] == "USER_ACCOUNT"
        assert not any(any(noise in node["name"].lower() for noise in ("statement", "who submitted", "references", "based on")) and node["entity_type"] == "PERSON" for node in nodes.values())
        assert {"ASSIGNED_TO", "ENTERED_LOCATION", "SENDER_OF", "AMENDED_BY_ACCOUNT", "CONTACT_EVENT"} == {edge["type"] for edge in edges}
        for edge in edges:
            assert edge["source_entity_id"] in nodes and edge["target_entity_id"] in nodes
            assert edge["evidence_document_id"]
            assert edge["evidence_text"]
            assert edge["verification_status"]
        assert any(edge["type"] == "ENTERED_LOCATION" and edge["verification_status"] == "WITNESS_REPORTED" and "witness.txt" in edge["evidence"] for edge in edges)
        person_graph = client.get("/api/graph", params={"case_id": case_id, "graph_type": "person"}).json()
        assert all(node["entity_type"] == "PERSON" for node in person_graph["nodes"])
        assert {edge["type"] for edge in person_graph["edges"]} == {"CONTACT_EVENT"}
        assert not any(edge["type"] == "HAS_EVIDENCE_ENTITY" for edge in graph["edges"])

        queries = [
            ("Who is listed as the driver of V-204?", "Arjun Mehta", "DR-8841"),
            ("Which organization sent shipment SH-771?", "Northstar Components Ltd.", "DR-8841"),
            ("Which account amended DI-551?", "operator-17", "AM-052"),
            ("What evidence supports the recorded vehicle entry?", "WITNESS_REPORTED", "witness.txt"),
            ("What information remains unverified?", "WITNESS_REPORTED", "witness.txt"),
        ]
        for question, expected, source in queries:
            answer = local_store.copilot_answer(question, case_id)
            assert expected in answer["answer"]
            assert source in answer["answer"]
        absent = local_store.copilot_answer("Who is the driver of Vehicle V-999?", case_id)
        assert "no stored driver assignment" in absent["answer"].lower()
        persisted = client.get("/api/graph", params={"case_id": case_id}).json()
        assert persisted["total_edges"] == 5

    with local_store.connect() as conn:
        assert conn.execute("SELECT 1 FROM cases WHERE case_id='CASE-KEEP'").fetchone()
        assert conn.execute("SELECT 1 FROM entities WHERE case_id='CASE-KEEP' AND entity_id='KEEP-P1'").fetchone()


def test_scoped_synthetic_reprocess_preserves_case_and_source_documents(tmp_path, monkeypatch):
    monkeypatch.setattr(local_store, "DB_PATH", tmp_path / "atlas.sqlite3")
    monkeypatch.setattr(local_store, "UPLOAD_DIR", tmp_path / "uploads")
    local_store.initialize()
    case_id = "CASE-AT-2026-041-MIGRATION"
    entity_csv = "entity_id,entity_name,entity_type,identifier,notes\nP-001,Arjun Mehta,PERSON,PERSON-001,Driver AT-2026-041\nV-001,Vehicle V-204,VEHICLE,V-204,Vehicle\n"
    relationship_csv = "relationship_id,source,target,type,evidence,status,note\nR-1,Arjun Mehta,Vehicle V-204,ASSIGNED_TO,DR-8841,RECORDED,Dispatch record lists driver\n"
    with local_store.connect() as conn:
        conn.execute("INSERT INTO cases(case_id,title,metadata) VALUES(?,?,?)", (case_id, "Synthetic AT-2026-041", "{}"))
        conn.execute("INSERT INTO cases(case_id,title,metadata) VALUES(?,?,?)", ("CASE-OTHER", "Keep existing data", "{}"))
        conn.execute("INSERT INTO entities(entity_id,case_id,label,name,properties) VALUES(?,?,?,?,?)", ("BAD-PERSON", case_id, "Person", "who submitted statement", "{}"))
        conn.execute("INSERT INTO entities(entity_id,case_id,label,name,properties) VALUES(?,?,?,?,?)", ("OTHER-P1", "CASE-OTHER", "Person", "Preserved Person", "{}"))
        for filename, content in (("06_Entity_Register.csv", entity_csv), ("07_Relationship_Register.csv", relationship_csv), ("dispatch.txt", "Synthetic DR-8841 lists Arjun Mehta as driver of Vehicle V-204.")):
            sha = __import__("hashlib").sha256(content.encode()).hexdigest()
            doc_id = __import__("hashlib").sha256(f"{case_id}:{sha}".encode()).hexdigest()
            conn.execute("INSERT INTO documents(document_id,case_id,filename,sha256,content,storage_path) VALUES(?,?,?,?,?,NULL)", (doc_id, case_id, filename, sha, content))
    result = local_store.reprocess_registered_case(case_id, "AT-2026-041")
    assert result["entities_written"] == 3 and result["relationships_written"] == 1 and result["documents_preserved"] == 3
    graph = local_store.graph_data(case_id)
    assert {node["name"] for node in graph["nodes"]} >= {"Arjun Mehta", "Vehicle V-204", "Synthetic AT-2026-041"}
    assert graph["edges"][0]["type"] == "ASSIGNED_TO"
    with local_store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM documents WHERE case_id=?", (case_id,)).fetchone()[0] == 3
        assert conn.execute("SELECT 1 FROM entities WHERE case_id='CASE-OTHER' AND entity_id='OTHER-P1'").fetchone()


def test_person_extraction_rejects_witness_and_heading_fragments():
    assert is_valid_person_name("Arjun Mehta")
    for fragment in ("Statement Reference", "who submitted statement", "statement references gate log", "statement is an attributed report", "based on this synthetic dataset"):
        assert not is_valid_person_name(fragment), fragment
    engine = CaseIngestionEngine(api_key=None)
    engine.parse_unstructured_text("Witness Statement Reference\nWitness who submitted statement\nWitness statement references gate log\nI, Nisha Rao, state that I saw a vehicle.")
    # A name mentioned in prose without an explicit structured/name-role cue is
    # not promoted by this conservative extractor; the entity register supplies it.
    assert engine.people_by_id == {}
