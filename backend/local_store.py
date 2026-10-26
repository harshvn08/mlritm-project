"""Credential-free local case graph persistence using SQLite."""
import json
import os
import sqlite3
import hashlib
import csv
import io
from pathlib import Path
from typing import Any
import networkx as nx
import re
from backend.storage_paths import default_data_dir
from backend.config import settings
from backend.services.person_roles import extract_roles, role_properties
from backend.services.evidence_person_mapping import apply_person_mappings
from backend.case_access import may_read, require_case

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = default_data_dir()
DB_PATH = Path(os.environ.get("ATLAS_SQLITE_PATH") or settings.SQLITE_PATH or DATA_DIR / "atlas_local.sqlite3")
UPLOAD_DIR = Path(os.environ.get("ATLAS_UPLOAD_DIR", DATA_DIR / "uploads"))


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def initialize():
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS cases(case_id TEXT PRIMARY KEY, title TEXT NOT NULL, metadata TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS documents(document_id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE, filename TEXT NOT NULL, sha256 TEXT NOT NULL, content TEXT NOT NULL, storage_path TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(case_id, sha256));
        CREATE TABLE IF NOT EXISTS entities(entity_id TEXT NOT NULL, case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE, label TEXT NOT NULL, name TEXT NOT NULL, properties TEXT NOT NULL, PRIMARY KEY(case_id, entity_id));
        CREATE TABLE IF NOT EXISTS relationships(relationship_id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE, source TEXT NOT NULL, target TEXT NOT NULL, rel_type TEXT NOT NULL, properties TEXT NOT NULL, evidence TEXT NOT NULL, certainty TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_entities_case ON entities(case_id);
        CREATE INDEX IF NOT EXISTS idx_relationships_case ON relationships(case_id);
        """)
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(documents)").fetchall()}
        if "storage_path" not in columns:
            conn.execute("ALTER TABLE documents ADD COLUMN storage_path TEXT")


def _json(value: Any):
    return json.dumps(value, ensure_ascii=False, default=str)


_REGISTER_TYPE_LABELS = {
    "PERSON": "Person", "ORGANIZATION": "Organization", "LOCATION": "Location",
    "VEHICLE": "Vehicle", "SHIPMENT": "Shipment", "DOCUMENT": "Document",
    "USER_ACCOUNT": "UserAccount", "IDENTIFIER": "Identifier", "PHONE": "Phone",
    "EMAIL": "Email", "BANK_ACCOUNT": "BankAccount",
}


def _parse_registers(case_id: str, docs: list[tuple[str, str, str, str, bytes]]):
    """Extract explicitly structured entity and relationship registers with provenance."""
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    aliases: dict[str, str] = {}
    for filename, digest, text, _path, _raw in docs:
        if Path(filename).suffix.lower() != ".csv":
            continue
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
        headers = {str(h or "").strip().lower().replace(" ", "_") for h in (reader.fieldnames or [])}
        rows = list(reader)
        document_id = hashlib.sha256(f"{case_id}:{digest}".encode()).hexdigest()
        if {"entity_id", "entity_name", "entity_type"}.issubset(headers):
            for raw_row in rows:
                row = {str(k or "").strip().lower().replace(" ", "_"): str(v or "").strip() for k, v in raw_row.items()}
                external_id, name, raw_type = row.get("entity_id"), row.get("entity_name"), row.get("entity_type", "")
                label = _REGISTER_TYPE_LABELS.get(raw_type.upper())
                if not name or not label:
                    continue
                entity_id = external_id or hashlib.sha256(f"{case_id}:{raw_type}:{name}".encode()).hexdigest()[:16]
                properties = {
                    **{k: v for k, v in row.items() if v},
                    "entity_type": raw_type.upper(), "canonical_name": name,
                    "source_document_id": document_id, "source_document": filename,
                    "evidence_text": ", ".join(f"{k}={v}" for k, v in row.items() if v),
                }
                nodes[entity_id] = {"id": entity_id, "labels": [label], "entity_type": raw_type.upper(), "name": name,
                    "case_ids": [case_id], "properties": properties}
                for value in (name, external_id, row.get("identifier")):
                    if value:
                        aliases[value.casefold()] = entity_id
                if row.get("identifier"):
                    aliases[row["identifier"].casefold()] = entity_id
        elif {"relationship_id", "source", "target", "type", "evidence"}.issubset(headers):
            relation_rows = rows
            for raw_row in relation_rows:
                row = {str(k or "").strip().lower().replace(" ", "_"): str(v or "").strip() for k, v in raw_row.items()}
                for endpoint in (row.get("source", ""), row.get("target", "")):
                    if endpoint and endpoint.casefold() not in aliases:
                        # Relationship registers may reference an evidence object not
                        # separately listed in the entity register (for example GL-219).
                        prefix = endpoint.split(" ", 1)[0].casefold()
                        if prefix in {"vehicle"}: label, etype = "Vehicle", "VEHICLE"
                        elif prefix in {"shipment"}: label, etype = "Shipment", "SHIPMENT"
                        elif prefix in {"delivery", "dispatch", "gate"}: label, etype = "Document", "DOCUMENT"
                        elif re.fullmatch(r"operator[-_].+", endpoint, re.I): label, etype = "UserAccount", "USER_ACCOUNT"
                        else: label, etype = "Identifier", "IDENTIFIER"
                        token = endpoint.rsplit(" ", 1)[-1] if " " in endpoint else endpoint
                        entity_id = f"{etype}_{hashlib.sha1(endpoint.casefold().encode()).hexdigest()[:14]}"
                        nodes.setdefault(entity_id, {"id": entity_id, "labels": [label], "entity_type": etype, "name": endpoint,
                            "case_ids": [case_id], "properties": {"canonical_name": endpoint, "identifier": token,
                                "entity_type": etype, "case_id": case_id, "source_document_id": document_id,
                                "source_document": filename, "evidence_text": endpoint}})
                        aliases[endpoint.casefold()] = entity_id
                        aliases[token.casefold()] = entity_id
            for row in relation_rows:
                row = {str(k or "").strip().lower().replace(" ", "_"): str(v or "").strip() for k, v in row.items()}
                source_id, target_id = aliases.get(row.get("source", "").casefold()), aliases.get(row.get("target", "").casefold())
                if not source_id or not target_id or not row.get("type"):
                    continue
                refs = [x.strip() for x in row.get("evidence", "").split(";") if x.strip()]
                evidence_docs = []
                for ref in refs:
                    found = [hashlib.sha256(f"{case_id}:{digest}".encode()).hexdigest()
                        for name, digest, content, *_ in docs
                        if "register" not in name.casefold() and ref.casefold() in content.casefold()]
                    evidence_docs.extend(found)
                evidence_docs = list(dict.fromkeys(evidence_docs))
                note = row.get("note", "")
                status_value = row.get("status", "UNVERIFIED").upper().replace("-", "_")
                evidence_text = "; ".join(x for x in (note, "Evidence reference: " + ", ".join(refs) if refs else "") if x)
                rel_type = row["type"].upper().replace(" ", "_")
                evidence = f"{', '.join(refs) or 'register reference'} · {', '.join(Path(name).name for name, digest, content, *_ in docs if hashlib.sha256(f'{case_id}:{digest}'.encode()).hexdigest() in evidence_docs)}"
                edge_props = {"relationship_id": row.get("relationship_id"), "source_entity_id": source_id,
                    "target_entity_id": target_id, "relationship_type": rel_type,
                    "evidence_document_ids": evidence_docs, "evidence_document_id": evidence_docs[0] if evidence_docs else None,
                    "evidence_text": evidence_text, "verification_status": status_value,
                    "source_register_document_id": document_id, "source_register": filename,
                    "recorded_note": note}
                edge_id = hashlib.sha256(_json([case_id, source_id, target_id, rel_type, refs, status_value]).encode()).hexdigest()
                edges.append({"id": edge_id, "source": source_id, "target": target_id, "type": rel_type,
                    "source_entity_id": source_id, "target_entity_id": target_id, "relationship_type": rel_type,
                    "evidence_document_id": evidence_docs[0] if evidence_docs else None,
                    "evidence_text": evidence_text, "verification_status": status_value,
                    "properties": {**edge_props, "evidence": evidence}, "evidence": evidence,
                    "certainty": "witness_reported" if "WITNESS" in status_value else "recorded" if status_value == "RECORDED" else "unverified"})
    # Add only high-confidence identifier patterns with explicit context. In
    # particular, arbitrary digit groups and dates are not phone entities.
    for filename, digest, text, _path, _raw in docs:
        doc_id=hashlib.sha256(f"{case_id}:{digest}".encode()).hexdigest()
        for label, pattern in (("Email", r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
            ("Phone", r"(?i)\b(?:phone|mobile|msisdn|telephone)(?:\s+number)?\s*[:#-]?\s*(\+?\d[\d ()-]{6,}\d)")):
            for match in re.finditer(pattern, text, re.IGNORECASE):
                value=(match.group(1) if label=="Phone" else match.group(0)).strip()
                normalized=re.sub(r"[\s()-]", "", value) if label=="Phone" else value
                if label=="Phone" and not 8 <= sum(c.isdigit() for c in normalized) <= 15:
                    continue
                entity_id=f"{label.upper()}_{hashlib.sha1(normalized.casefold().encode()).hexdigest()[:14]}"
                nodes.setdefault(entity_id,{"id":entity_id,"labels":[label],"entity_type":label.upper(),"name":normalized,
                    "case_ids":[case_id],"properties":{"entity_type":label.upper(),"canonical_name":normalized,"value":normalized,
                        "source_document_id":doc_id,"source_document":filename,"evidence_text":match.group(0)}})
    return nodes, edges


def register_documents_present(uploaded_files: list[tuple[str, bytes, str]]) -> bool:
    for filename, _raw, text in uploaded_files:
        if Path(filename).suffix.lower() != ".csv":
            continue
        try:
            headers = {str(x).strip().lower().replace(" ", "_") for x in next(csv.reader(io.StringIO(text.lstrip("\ufeff"))))}
        except (StopIteration, csv.Error):
            continue
        if {"entity_id", "entity_name", "entity_type"}.issubset(headers) or {"relationship_id", "source", "target", "type", "evidence"}.issubset(headers):
            return True
        if {"entity_name", "entity_type", "relationship", "related_entity"}.issubset(headers):
            return True
    return False


def ingest(case_id: str, title: str, case_data, uploaded_files: list[tuple[str, bytes, str]], uploaded_by: str | None = None):
    initialize()
    # Reparse the case's complete evidence inventory so later files cannot reset
    # parser-local person IDs, roles, metadata, or earlier source associations.
    with connect() as conn:
        existing = conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        previous = conn.execute("SELECT * FROM documents WHERE case_id=? ORDER BY created_at,document_id", (case_id,)).fetchall()
    if existing:
        old_meta = json.loads(existing["metadata"])
        incoming_hashes = {hashlib.sha256(raw).hexdigest() for _, raw, _ in uploaded_files}
        uploaded_files = [(row["filename"], Path(row["storage_path"]).read_bytes() if row["storage_path"] and Path(row["storage_path"]).exists() else row["content"].encode(), row["content"])
                          for row in previous if row["sha256"] not in incoming_hashes] + uploaded_files
        from backend.services.ingestion_engine import CaseIngestionEngine
        from backend.services.schema_mapper import map_ingestion_to_graph_data
        engine = CaseIngestionEngine()
        engine.api_key = None  # Local evidence reprocessing stays deterministic even when Copilot has a key.
        for filename, raw, text in uploaded_files:
            if not register_documents_present([(filename, raw, text)]):
                engine.parse_file(filename, text)
        case_data = map_ingestion_to_graph_data(engine.consolidate().model_dump())
        title = existing["title"]
        uploaded_by = old_meta.get("uploaded_by") or uploaded_by
    data = case_data.model_dump(mode="json")
    meta = data["case_metadata"]
    if existing:
        meta = {**old_meta, **{k: v for k, v in meta.items() if v is not None and k not in {"case_id", "case_name", "uploaded_by"}}}
    meta.update({"case_id": case_id, "case_name": title, "uploaded_by": uploaded_by or meta.get("uploaded_by")})
    data["case_metadata"] = meta
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    docs: list[tuple[str, str, str, str, bytes]] = []
    for filename, content, extracted_text in uploaded_files:
        digest = hashlib.sha256(content).hexdigest()
        per_case_digest = hashlib.sha256(f"{case_id}:{digest}".encode()).hexdigest()
        target = UPLOAD_DIR / f"{per_case_digest}{Path(filename).suffix.lower()[:10]}"
        docs.append((filename, digest, extracted_text, str(target), content))
    for collection, label, id_key, name_key in [
        ("people", "Person", "person_id", "name"), ("phones", "Phone", "phone_number", "phone_number"),
        ("bank_accounts", "BankAccount", "account_number", "account_number"), ("vehicles", "Vehicle", "vin", "license_plate"),
        ("social_handles", "SocialHandle", "handle_id", "handle"), ("ip_addresses", "IPAddress", "ip_address", "ip_address"),
        ("locations", "Location", "location_id", "name"), ("cell_towers", "CellTower", "cell_tower_id", "cell_tower_id")]:
        for item in data["entities"].get(collection, []):
            eid = str(item.get(id_key) or item.get(name_key) or hashlib.sha1(_json(item).encode()).hexdigest()[:12])
            entity_name = str(item.get(name_key) or eid)
            tokens = [entity_name, eid] + [str(item[k]) for k in ("phone_number", "account_number", "email", "handle", "ip_address") if item.get(k)]
            source_docs = [d[0] for d in docs if any(token and token.casefold() in d[2].casefold() for token in tokens)]
            nodes[eid] = {"id": eid, "labels": [label], "entity_type": label.upper(), "name": entity_name, "case_ids": [case_id], "properties": {**item, "entity_type": label.upper(), "canonical_name": entity_name, "source_documents": source_docs}}
    register_nodes, register_edges = _parse_registers(case_id, docs)
    # An explicit entity register is authoritative over heuristic extraction for
    # this case. It restores canonical IDs/types and prevents narrative pseudo-names.
    registered_by_name = {n["name"].casefold():n for n in register_nodes.values()}
    for existing_id, existing_node in list(nodes.items()):
        registered=registered_by_name.get(str(existing_node.get("name", "")).casefold())
        if existing_id not in register_nodes and registered:
            if set(existing_node.get("labels", [])) & set(registered.get("labels", [])):
                old_props=existing_node.get("properties", {})
                new_props=registered.setdefault("properties", {})
                registered["properties"]={**old_props, **new_props,
                    "source_documents":list(dict.fromkeys(old_props.get("source_documents",[])+[x for x in [old_props.get("source_document")] if x]+new_props.get("source_documents",[])+[x for x in [new_props.get("source_document")] if x]))}
            del nodes[existing_id]
    nodes.update(register_nodes)
    assertions = extract_roles([(filename, text) for filename, _, text, _, _ in docs])
    for name, *_ in assertions:
        if not any(n["name"].casefold() == name.casefold() and "Person" in n["labels"] for n in nodes.values()):
            eid = "PERSON_" + hashlib.sha256(f"{case_id}:{name.casefold()}".encode()).hexdigest()[:20]
            nodes[eid] = {"id": eid, "labels": ["Person"], "name": name, "case_ids": [case_id], "properties": {}}
    edges.extend(apply_person_mappings(case_id, docs, nodes))
    for node in nodes.values():
        if "Person" in node["labels"]:
            explicit = [a for a in assertions if a[0].casefold() == node["name"].casefold()]
            node["properties"].update(role_properties(explicit))
    for _, _, text, _, _ in docs:
        fir = re.search(r"\bFIR\s*(?:number|no\.?|#)?\s*[:#-]?\s*((?=[^\s;,]*\d)[A-Z0-9][A-Z0-9/_-]*)", text, re.I)
        category = re.search(r"(?:crime\s+(?:classification|category|type))\s*:\s*([^\r\n;]+)", text, re.I)
        if fir: meta["fir_number"] = fir.group(1)
        if category: meta["case_type"] = meta["crime_category"] = category.group(1).strip()
    if meta.get("case_type") == "GENERAL_INVESTIGATION": meta["case_type"] = None
    if meta.get("summary", "") and "FIR number: N/A" in meta["summary"]: meta["summary"] = None
    edges.extend(register_edges)
    phone_ids = {str(n.get("properties", {}).get("phone_number") or n.get("name")): n["id"]
        for n in nodes.values() if "Phone" in n.get("labels", [])}
    account_ids = {str(n.get("properties", {}).get("account_number") or n.get("name")): n["id"]
        for n in nodes.values() if "BankAccount" in n.get("labels", [])}
    # Conservative local text recognition. These are observations from a cited file,
    # not accusations; relationship candidates are never inferred from co-occurrence.
    for filename, _, text, _, _ in docs:
        evidence = f"Extracted from {filename}"
        patterns = [
            ("Email", r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
            ("IPAddress", r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
            ("Phone", r"(?<!\d)(?:\+?\d[\d ()-]{7,}\d)(?!\d)"),
        ("Location", r"(?i)\b(?:location|address|city)\s*:\s*([^\r\n,;.]{2,100})"),
        ("Organization", r"(?i)\b(?:organization|organisation|company|employer)\s*:\s*([^\r\n,;.]{2,100})"),
        ]
        for label, pattern in patterns:
            for value in re.findall(pattern, text, re.IGNORECASE):
                normalized = re.sub(r"\s+", "", value) if label == "Phone" else value
                if label == "Phone" and re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2})?", value.strip()): continue
                if label == "Phone" and not 8 <= sum(ch.isdigit() for ch in normalized) <= 15: continue
                if label == "IPAddress" and any(int(part) > 255 for part in value.split(".")): continue
                eid = f"{label.upper()}_{hashlib.sha1(normalized.lower().encode()).hexdigest()[:14]}"
                nodes.setdefault(eid, {"id": eid, "labels": [label], "name": normalized, "case_ids": [case_id], "properties": {"value": normalized, "source_document": filename, "evidence": evidence}})
    nodes[case_id] = {"id": case_id, "labels": ["Case"], "name": title, "case_ids": [case_id], "properties": meta}
    # Case membership and document provenance live on entity/document metadata. They
    # are deliberately not graph edges; spokes falsely imply entity association.
    for node in nodes.values():
        if "Case" not in node["labels"]:
            node["properties"].setdefault("case_id", case_id)
    for rel in data["relationships"].get("communications", []):
        src = rel.get("source_phone"); dst = rel.get("target_phone")
        if src and dst:
            src = phone_ids.get(str(src), str(src)); dst = phone_ids.get(str(dst), str(dst))
            source_file = next((d[0] for d in docs if Path(d[0]).suffix.lower() == ".csv" and any(k in d[0].lower() for k in ("cdr", "call", "phone"))), next((d[0] for d in docs if Path(d[0]).suffix.lower() == ".csv"), "uploaded CDR document"))
            reference = rel.get("source_record_id") or rel.get("call_id") or rel.get("communication_id") or source_file
            edges.append({"source": src, "target": dst, "type": "CALLED", "properties": {**rel, "source_document": source_file}, "evidence": f"{source_file} · {reference}", "certainty": "observed"})
    for rel in data["relationships"].get("transactions", []):
        src = rel.get("source_account"); dst = rel.get("target_account")
        if src and dst:
            src = account_ids.get(str(src), str(src)); dst = account_ids.get(str(dst), str(dst))
            source_file = next((d[0] for d in docs if Path(d[0]).suffix.lower() == ".csv" and any(k in d[0].lower() for k in ("bank", "financ", "transaction", "transfer"))), next((d[0] for d in docs if Path(d[0]).suffix.lower() == ".csv"), "uploaded transaction document"))
            reference = rel.get("source_record_id") or rel.get("transaction_id") or rel.get("reference_no") or source_file
            edges.append({"source": src, "target": dst, "type": "TRANSFERRED_TO", "properties": {**rel, "source_document": source_file}, "evidence": f"{source_file} · {reference}", "certainty": "observed"})
    # Explicit owner references in the structured data support person/identifier
    # associations. They are labeled neutrally and never interpreted as wrongdoing.
    for node in list(nodes.values()):
        if "Person" in node["labels"]: continue
        owner_id = node["properties"].get("owner_person_id") or node["properties"].get("owner_id")
        if owner_id and owner_id in nodes:
            edges.append({"source": owner_id, "target": node["id"], "type": "ASSOCIATED_WITH", "properties": {"association": "explicit owner field", "source_documents": node["properties"].get("source_documents", [])}, "evidence": "; ".join(node["properties"].get("source_documents", [])) or "Structured uploaded record", "certainty": "recorded"})
    # Co-use of a phone is a candidate relationship, not a proven personal or criminal link.
    phone_owners: dict[str, list[str]] = {}
    for node in nodes.values():
        for phone in node["properties"].get("phone_numbers", []) if "Person" in node["labels"] else []:
            phone_owners.setdefault(str(phone), []).append(node["id"])
        if "Phone" in node["labels"]:
            owner=node["properties"].get("owner_person_id") or node["properties"].get("owner_id")
            phone=node["properties"].get("phone_number") or node["name"]
            if owner and owner in nodes: phone_owners.setdefault(str(phone), []).append(owner)
    for phone, owners in phone_owners.items():
        for i, source in enumerate(sorted(set(owners))):
            for target in sorted(set(owners))[i + 1:]:
                edges.append({"source": source, "target": target, "type": "SHARED_PHONE", "properties": {"phone": phone}, "evidence": f"Phone identifier {phone}", "certainty": "candidate"})
    deduped: dict[tuple, dict] = {}
    for edge in edges:
        key = (edge["source"], edge["target"], edge["type"], edge.get("evidence") or edge.get("properties", {}).get("evidence"))
        if key in deduped:
            props = deduped[key]["properties"]
            props["record_count"] = int(props.get("record_count", 1)) + int(edge.get("properties", {}).get("record_count", 1))
            continue
        edge["properties"] = {**edge.get("properties", {}), "record_count": 1}
        deduped[key] = edge
    edges = list(deduped.values())
    for edge in edges:
        edge["id"] = hashlib.sha1(_json([case_id, edge["source"], edge["target"], edge["type"], edge["properties"]]).encode()).hexdigest()
        edge["properties"] = {**edge.get("properties", {}), "evidence": edge.get("evidence", "Uploaded document evidence"), "certainty": edge.get("certainty", "observed")}
    graph = nx.MultiGraph()
    graph.add_nodes_from(nodes)
    graph.add_edges_from((e["source"], e["target"]) for e in edges)
    created_paths = []
    try:
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO cases(case_id,title,metadata) VALUES(?,?,?) ON CONFLICT(case_id) DO UPDATE SET title=excluded.title, metadata=excluded.metadata", (case_id, title, _json(meta)))
            # Only derived data for this case is rebuilt, inside the same transaction.
            conn.execute("DELETE FROM relationships WHERE case_id=?", (case_id,))
            conn.execute("DELETE FROM entities WHERE case_id=?", (case_id,))
            for filename, digest, text, stored_path, raw_content in docs:
                document_id = hashlib.sha256(f"{case_id}:{digest}".encode()).hexdigest()
                if not conn.execute("SELECT 1 FROM documents WHERE document_id=?", (document_id,)).fetchone():
                    temp = Path(stored_path + ".tmp")
                    temp.write_bytes(raw_content)
                    temp.replace(stored_path)
                    created_paths.append(stored_path)
                    conn.execute("INSERT INTO documents(document_id,case_id,filename,sha256,content,storage_path) VALUES(?,?,?,?,?,?)", (document_id, case_id, filename, digest, text, stored_path))
            for node in nodes.values():
                conn.execute("INSERT INTO entities(entity_id,case_id,label,name,properties) VALUES(?,?,?,?,?) ON CONFLICT(case_id,entity_id) DO UPDATE SET label=excluded.label,name=excluded.name,properties=excluded.properties", (node["id"], case_id, node["labels"][0], node["name"], _json(node["properties"])))
            for edge in edges:
                conn.execute("INSERT OR IGNORE INTO relationships(relationship_id,case_id,source,target,rel_type,properties,evidence,certainty) VALUES(?,?,?,?,?,?,?,?)", (edge["id"], case_id, edge["source"], edge["target"], edge["type"], _json(edge["properties"]), edge["properties"]["evidence"], edge["properties"]["certainty"]))
            conn.commit()
    except Exception:
        for path in created_paths:
            Path(path).unlink(missing_ok=True)
        raise
    return {"nodes": sum("Case" not in n["labels"] for n in nodes.values()), "relationships": len(edges), "source_records": len(docs)}, data


def reprocess_registered_case(case_id: str, expected_marker: str):
    """Explicitly rebuild one case's derived graph from its stored register documents.

    Caller must provide a marker that is present in the case documents. Source
    documents and the case row are preserved; only that case's derived nodes and
    links are replaced, atomically.
    """
    initialize()
    with connect() as conn:
        case = conn.execute("SELECT title,metadata FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if not case:
            raise ValueError(f"Case {case_id!r} was not found")
        rows = conn.execute("SELECT document_id,filename,sha256,content,storage_path FROM documents WHERE case_id=? ORDER BY filename", (case_id,)).fetchall()
    docs=[]
    for row in rows:
        content=Path(row["storage_path"]).read_bytes() if row["storage_path"] and Path(row["storage_path"]).exists() else row["content"].encode("utf-8")
        text=row["content"]
        docs.append((row["filename"],row["sha256"],text,row["storage_path"] or "",content))
    combined="\n".join(d[2] for d in docs)
    if expected_marker.casefold() not in combined.casefold():
        raise ValueError(f"Expected marker {expected_marker!r} was not found in stored source documents")
    names={Path(d[0]).name.casefold() for d in docs}
    if not any("entity_register" in name for name in names) or not any("relationship_register" in name for name in names):
        raise ValueError("This scoped migration requires both entity and relationship register documents")
    nodes,edges=_parse_registers(case_id,docs)
    if not nodes or not edges:
        raise ValueError("The stored register documents did not produce entities and relationships")
    nodes[case_id]={"id":case_id,"labels":["Case"],"entity_type":"CASE","name":case["title"],"case_ids":[case_id],"properties":json.loads(case["metadata"])}
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM relationships WHERE case_id=?",(case_id,))
        conn.execute("DELETE FROM entities WHERE case_id=?",(case_id,))
        for node in nodes.values():
            conn.execute("INSERT INTO entities(entity_id,case_id,label,name,properties) VALUES(?,?,?,?,?)",
                (node["id"],case_id,node["labels"][0],node["name"],_json(node["properties"])))
        for edge in edges:
            conn.execute("INSERT INTO relationships(relationship_id,case_id,source,target,rel_type,properties,evidence,certainty) VALUES(?,?,?,?,?,?,?,?)",
                (edge["id"],case_id,edge["source"],edge["target"],edge["type"],_json(edge["properties"]),edge["evidence"],edge["certainty"]))
        conn.commit()
    return {"case_id":case_id,"entities_written":len(nodes),"relationships_written":len(edges),"documents_preserved":len(docs)}


def list_cases(limit=50, offset=0, status_filter=None, uploaded_by=None):
    with connect() as c:
        rows = c.execute("SELECT c.*, (SELECT count(*) FROM entities e WHERE e.case_id=c.case_id AND e.label!='Case') total_entities, (SELECT count(*) FROM documents d WHERE d.case_id=c.case_id) doc_count FROM cases c ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    cases = [{"status": "OPEN", **json.loads(r["metadata"]), "case_id": r["case_id"], "case_name": r["title"], "created_at": r["created_at"], "total_entities": r["total_entities"], "evidences_collected": [{"name": x["filename"], "type": Path(x["filename"]).suffix[1:].upper()} for x in documents(r["case_id"])]} for r in rows]
    cases = [case for case in cases if may_read(case)]
    if status_filter: cases = [c for c in cases if str(c.get("status", "OPEN")).lower() == status_filter.lower()]
    if uploaded_by: cases = [c for c in cases if str(c.get("uploaded_by") or "").lower() == uploaded_by.lower()]
    return cases


def documents(case_id):
    with connect() as c: return c.execute("SELECT * FROM documents WHERE case_id=? ORDER BY created_at", (case_id,)).fetchall()


def case_detail(case_id):
    require_case(case_id)
    with connect() as conn:
        row = conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
    if row is None: return None
    case = {**json.loads(row["metadata"]), "case_id": row["case_id"], "case_name": row["title"], "created_at": row["created_at"]}
    graph = graph_data(case_id)
    case["total_entities"] = sum("Case" not in n["labels"] for n in graph["nodes"])
    people = [{"entity_id": n["id"], "name": n["name"], **n["properties"]} for n in graph["nodes"] if "Person" in n["labels"]]
    case["people"] = people
    for field, role in (("primary_suspects", "suspect"), ("victims", "victim"), ("witnesses", "witness"), ("persons_of_interest", "person_of_interest")):
        case[field] = [p for p in people if p.get("role") == role and not p.get("role_conflict")]
    case["role_conflicts"] = [p for p in people if p.get("role_conflict")]
    case["total_relationships"] = len(graph["edges"])
    case["analysis_status"] = "processed"
    case["summary"] = case.get("summary") or "Locally processed evidence; extracted links require analyst verification."
    case["evidences_collected"] = [{"name": d["filename"], "type": Path(d["filename"]).suffix[1:].upper()} for d in documents(case_id)]
    case["documents"] = [{"document_id": d["document_id"], "filename": d["filename"], "sha256": d["sha256"], "created_at": d["created_at"], "content": d["content"], "analysis_status": "processed"} for d in documents(case_id)]
    return case


def reprocess_case(case_id):
    """Rebuild derived analysis using preserved documents, without deleting evidence."""
    case = case_detail(case_id)
    if case is None: return None
    rows = documents(case_id)
    if not rows: raise ValueError("No stored source documents are available for reprocessing.")
    from backend.models.case_input import CaseData, CaseMetadata
    placeholder = CaseData(case_metadata=CaseMetadata(case_id=case_id, case_name=case["case_name"]))
    files = [(d["filename"], Path(d["storage_path"]).read_bytes() if d["storage_path"] and Path(d["storage_path"]).exists() else d["content"].encode(), d["content"]) for d in rows]
    ingest(case_id, case["case_name"], placeholder, files, case.get("uploaded_by"))
    return case_detail(case_id)


def graph_data(case_id=None, limit=None, focus_entity_id=None, hops=1):
    if case_id: require_case(case_id)
    with connect() as c:
        allowed = {r["case_id"] for r in c.execute("SELECT case_id,metadata FROM cases").fetchall() if may_read(json.loads(r["metadata"]))}
        if case_id:
            ns=c.execute("SELECT * FROM entities WHERE case_id=?",(case_id,)).fetchall(); rs=c.execute("SELECT * FROM relationships WHERE case_id=?",(case_id,)).fetchall()
        else:
            ns=c.execute("SELECT * FROM entities").fetchall(); rs=c.execute("SELECT * FROM relationships").fetchall()
    ns = [r for r in ns if r["case_id"] in allowed]
    rs = [r for r in rs if r["case_id"] in allowed]
    nodes=[]
    for r in ns:
        properties=json.loads(r["properties"])
        nodes.append({"id":r["entity_id"],"labels":[r["label"]],"entity_type":properties.get("entity_type",r["label"].upper()),
            "name":properties.get("canonical_name",r["name"]),"canonical_name":properties.get("canonical_name",r["name"]),
            "case_ids":[r["case_id"]],"properties":properties})
    edge_map={}
    for r in rs:
        if r["rel_type"] == "HAS_EVIDENCE_ENTITY": continue
        properties=json.loads(r["properties"])
        edge={"id":r["relationship_id"],"source":r["source"],"target":r["target"],"type":r["rel_type"],"relationship_type":properties.get("relationship_type",r["rel_type"]),
            "source_entity_id":properties.get("source_entity_id",r["source"]),"target_entity_id":properties.get("target_entity_id",r["target"]),
            "evidence_document_id":properties.get("evidence_document_id"),"evidence_text":properties.get("evidence_text"),
            "verification_status":properties.get("verification_status",r["certainty"]),"properties":properties,"evidence":r["evidence"],"certainty":r["certainty"]}
        key=(edge["source"],edge["target"],edge["type"],edge["evidence"])
        if key in edge_map: edge_map[key]["properties"]["record_count"]=edge_map[key]["properties"].get("record_count",1)+1
        else: edge_map[key]=edge
    edges=list(edge_map.values())
    if focus_entity_id:
        reached={focus_entity_id}; frontier={focus_entity_id}
        for _ in range(max(1,min(2,int(hops)))):
            next_frontier=set()
            for edge in edges:
                if edge["source"] in frontier: next_frontier.add(edge["target"])
                if edge["target"] in frontier: next_frontier.add(edge["source"])
            next_frontier -= reached; reached |= next_frontier; frontier=next_frontier
        nodes=[n for n in nodes if n["id"] in reached]
        edges=[e for e in edges if e["source"] in reached and e["target"] in reached]
    if limit and len(nodes)>limit:
        degree={n["id"]:0 for n in nodes}
        for e in edges:
            degree[e["source"]]=degree.get(e["source"],0)+1; degree[e["target"]]=degree.get(e["target"],0)+1
        keep={n["id"] for n in sorted(nodes,key=lambda n:(-degree.get(n["id"],0),n["id"]))[:limit]}
        nodes=[n for n in nodes if n["id"] in keep]
        edges=[e for e in edges if e["source"] in keep and e["target"] in keep]
    known={n["id"] for n in nodes}
    edges=[e for e in edges if e["source"] in known and e["target"] in known]
    return {"case_id":case_id,"total_nodes":len(nodes),"total_edges":len(edges),"nodes":nodes,"edges":edges,"limited":bool(limit and len(ns)>limit),"focus_entity_id":focus_entity_id}


def copilot_answer(question: str, case_id: str | None):
    """Small deterministic local QA over persisted case graph and document records."""
    import re
    from difflib import SequenceMatcher
    q=question.strip(); low=q.lower()
    if not case_id:
        return {"answer":"Select a case in Graph Explorer first so I can answer from that case's stored records.","facts":[],"intent":"case_required","case_id":None}
    case=case_detail(case_id)
    if not case:
        return {"answer":f"No stored case with ID {case_id!r} was found.","facts":[],"intent":"case_not_found","case_id":case_id}
    graph=graph_data(case_id); nodes=graph["nodes"]; edges=graph["edges"]
    by_id={n["id"]:n for n in nodes}
    evidence=lambda e: e.get("evidence") or e.get("properties",{}).get("source_document") or "Evidence reference unavailable"
    doc_names={d["document_id"]:d["filename"] for d in documents(case_id)}
    def evidence_detail(edge):
        props=edge.get("properties",{})
        source_names=[doc_names[x] for x in props.get("evidence_document_ids",[]) if x in doc_names]
        ref=evidence(edge)
        if source_names:
            ref += " (source: " + ", ".join(dict.fromkeys(source_names)) + ")"
        return ref
    def find_entities(text):
        matches=[]
        for n in nodes:
            candidates=[n["name"],n["id"]]
            candidates += [str(v) for k,v in n.get("properties",{}).items() if k in ("phone_number","account_number","email","handle","organization","identifier","canonical_name") and isinstance(v,(str,int))]
            if any(c and str(c).lower() in text.lower() for c in candidates): matches.append(n)
        return sorted(matches,key=lambda n:max(len(str(v)) for v in [n["name"],n["id"]]),reverse=True)
    def path_answer():
        candidates=find_entities(q)
        # Prefer explicit 'between X and Y' fragments, then longest exact known-name mentions.
        between=re.search(r"between\s+(.+?)\s+and\s+(.+?)(?:[?.]|$)",q,re.I)
        endpoints=[]
        if between:
            for part in between.groups():
                found=sorted(nodes,key=lambda n:max(SequenceMatcher(None,part.lower(),str(n[k]).lower()).ratio() for k in ("name","id")),reverse=True)
                if found and max(SequenceMatcher(None,part.lower(),str(found[0][k]).lower()).ratio() for k in ("name","id"))>.58: endpoints.append(found[0])
        if len(endpoints)<2:
            unique=[]
            for n in candidates:
                if n["id"] not in [x["id"] for x in unique]: unique.append(n)
            endpoints=unique[:2]
        if len(endpoints)<2: return "Name two entities that appear in this case's records; no path endpoints can be resolved from the question.",[]
        g=nx.Graph()
        for e in edges: g.add_edge(e["source"],e["target"],record=e)
        try: route=nx.shortest_path(g,endpoints[0]["id"],endpoints[1]["id"])
        except (nx.NetworkXNoPath,nx.NodeNotFound): return f"No stored relationship path connects {endpoints[0]['name']} and {endpoints[1]['name']} in this case.",[]
        facts=[]
        for a,b in zip(route,route[1:]):
            rec=g[a][b]["record"]; facts.append(f"{by_id[a]['name']} —{rec['type']}→ {by_id[b]['name']} [{evidence(rec)}; {rec.get('certainty','recorded')}]")
        return "Recorded shortest path (each link is an evidence association, not proof of wrongdoing):\n"+"\n".join(facts),facts
    mentioned=find_entities(q)
    if "driver" in low and ("vehicle" in low or any("vehicle" in n.get("entity_type", "").lower() for n in mentioned)):
        vehicles=[n for n in mentioned if n.get("entity_type", "").upper()=="VEHICLE" or "Vehicle" in n["labels"]]
        target=vehicles[0] if vehicles else None
        explicit_vehicle = bool(re.search(r"\bV[-_ ]?\d+\b", q, re.I))
        rels=[e for e in edges if e["type"]=="ASSIGNED_TO" and (e["target"]==target["id"] if target else not explicit_vehicle)]
        facts=[f"{by_id[e['source']]['name']} —{e['type']}→ {by_id[e['target']]['name']} [{evidence_detail(e)}; {e.get('verification_status',e.get('certainty','recorded'))}]" for e in rels if e["source"] in by_id and e["target"] in by_id]
        answer=("Recorded driver assignment: "+"; ".join(facts)) if facts else "The selected case has no stored driver assignment for the requested vehicle."
        intent="driver_lookup"
    elif ("sent" in low or "sender" in low) and ("shipment" in low or any("shipment" in n.get("entity_type", "").lower() for n in mentioned)):
        shipments=[n for n in mentioned if n.get("entity_type", "").upper()=="SHIPMENT" or "Shipment" in n["labels"]]
        target=shipments[0] if shipments else None
        rels=[e for e in edges if e["type"]=="SENDER_OF" and (not target or e["target"]==target["id"])]
        facts=[f"{by_id[e['source']]['name']} —{e['type']}→ {by_id[e['target']]['name']} [{evidence_detail(e)}; {e.get('verification_status',e.get('certainty','recorded'))}]" for e in rels if e["source"] in by_id and e["target"] in by_id]
        answer=("Recorded shipment sender: "+"; ".join(facts)) if facts else "The selected case has no stored sender relationship for the requested shipment."
        intent="shipment_sender"
    elif ("amend" in low or "amended" in low) and ("account" in low or "user" in low):
        targets=[n for n in mentioned if n.get("entity_type", "").upper()=="DOCUMENT" or "Document" in n["labels"]]
        rels=[e for e in edges if e["type"]=="AMENDED_BY_ACCOUNT" and (not targets or e["target"] in {n["id"] for n in targets})]
        facts=[f"{by_id[e['source']]['name']} —{e['type']}→ {by_id[e['target']]['name']} [{evidence_detail(e)}; {e.get('verification_status',e.get('certainty','recorded'))}]" for e in rels if e["source"] in by_id and e["target"] in by_id]
        answer=("Recorded amending account: "+"; ".join(facts)+". The account record does not establish the real-world identity of its user.") if facts else "The selected case has no stored account-amendment relationship for the requested document."
        intent="amending_account"
    elif ("vehicle entry" in low or "entered" in low) and ("evidence" in low or "support" in low):
        rels=[e for e in edges if e["type"]=="ENTERED_LOCATION"]
        facts=[f"{by_id[e['source']]['name']} —{e['type']}→ {by_id[e['target']]['name']} [{evidence_detail(e)}; {e.get('verification_status',e.get('certainty','recorded'))}; {e.get('evidence_text') or e.get('properties',{}).get('evidence_text') or 'no excerpt stored'}]" for e in rels if e["source"] in by_id and e["target"] in by_id]
        answer=("Stored vehicle-entry support: "+"; ".join(facts)) if facts else "The selected case contains no stored ENTERED_LOCATION relationship."
        intent="vehicle_entry_evidence"
    elif "unverified" in low or "verify" in low or "unresolved" in low:
        uncertain=[e for e in edges if "WITNESS" in str(e.get("verification_status",e.get("certainty",""))).upper() or "UNVERIFIED" in str(e.get("verification_status",e.get("certainty",""))).upper() or "CANDIDATE" in str(e.get("verification_status",e.get("certainty",""))).upper()]
        facts=[f"{by_id[e['source']]['name']} —{e['type']}→ {by_id[e['target']]['name']} [{evidence_detail(e)}; {e.get('verification_status',e.get('certainty','recorded'))}]" for e in uncertain if e["source"] in by_id and e["target"] in by_id]
        unresolved=[n["name"]+": "+str(n["properties"].get("notes")) for n in nodes if "not established" in str(n.get("properties",{}).get("notes","" )).lower() or "not verified" in str(n.get("properties",{}).get("notes","" )).lower()]
        facts.extend(unresolved)
        answer="Unverified or witness-reported records requiring analyst verification: "+("; ".join(facts) if facts else "the selected case has no records explicitly marked unverified; absence of such a mark is not confirmation.")
        intent="unverified_records"
    elif any(term in low for term in ("shortest path","path between","connect ","how are")):
        answer,facts=path_answer(); intent="shortest_path"
    elif "main suspect" in low or "who is the suspect" in low:
        deg={n["id"]:0 for n in nodes}
        for e in edges:
            if e["source"] in deg: deg[e["source"]]+=1
            if e["target"] in deg: deg[e["target"]]+=1
        leaders=sorted((n for n in nodes if "Person" in n["labels"]),key=lambda n:deg[n["id"]],reverse=True)[:5]
        facts=[f"{n['name']}: {deg[n['id']]} recorded graph connections" for n in leaders if deg[n["id"]]]
        answer="Connectivity cannot establish who is a suspect. Most connected people are investigation leads only: "+("; ".join(facts) if facts else "no people have recorded relationships in this case.")
        intent="suspect_safety"
    elif "most connected" in low or "connections" in low or "degree" in low:
        deg={n["id"]:0 for n in nodes}
        for e in edges:
            if e["source"] in deg: deg[e["source"]]+=1
            if e["target"] in deg: deg[e["target"]]+=1
        top=sorted(nodes,key=lambda n:deg[n["id"]],reverse=True)[:8]
        facts=[f"{n['name']} ({n['labels'][0]}): {deg[n['id']]} stored relationships" for n in top if deg[n["id"]]]
        answer="Most connected entities by stored edge count (leads only; connectivity is not a suspicion or guilt assessment): "+("; ".join(facts) if facts else "the case has no stored relationships.")
        intent="degree_ranking"
    elif "missing" in low or "information" in low and "missing" in low:
        present={n["labels"][0] for n in nodes}
        absent=[x for x in ("Person","Phone","Email","Organization","Location","BankAccount","Vehicle") if x not in present]
        docs=case.get("evidences_collected",[])
        answer=f"Stored case coverage: {len(docs)} source documents, {len(nodes)-1} extracted entities, {len(edges)} recorded links. Entity types not currently represented: {', '.join(absent) if absent else 'none of these common types'}. This describes extraction coverage only; it cannot establish what evidence should exist."
        facts=[d["name"] for d in docs]; intent="coverage"
    elif "document" in low or "evidence" in low or "mention" in low:
        candidates=find_entities(q)
        if not candidates: answer="No named entity or identifier in that question matched the selected case's stored records."; facts=[]
        else:
            ent=candidates[0]; docs=ent.get("properties",{}).get("source_documents",[])
            if ent.get("properties",{}).get("source_document"): docs=list(set(docs+[ent["properties"]["source_document"]]))
            answer=f"{ent['name']} is present in stored entity records. Linked source documents: {', '.join(docs) if docs else 'no document-level reference was recorded for this entity'} ."
            facts=list(docs)
        intent="source_lookup"
    elif "organization" in low or "company" in low:
        candidates=find_entities(q); people=[n for n in candidates if "Person" in n["labels"]]
        person=people[0] if people else None
        rels=[e for e in edges if person and (e["source"]==person["id"] or e["target"]==person["id"]) and "Organization" in by_id.get(e["target"] if e["source"]==person["id"] else e["source"],{}).get("labels",[])]
        facts=[f"{by_id[e['source']]['name']} —{e['type']}→ {by_id[e['target']]['name']} [{evidence(e)}]" for e in rels]
        answer=(f"Recorded organization associations for {person['name']}: "+"; ".join(facts)) if facts else "The selected case contains no stored evidence-supported relationship between the named person and an organization. Mere co-occurrence in a document is not treated as an association."
        intent="organization_links"
    elif "phone" in low and ("share" in low or "shared" in low):
        matches=[e for e in edges if e["type"]=="SHARED_PHONE"]
        facts=[f"{by_id[e['source']]['name']} and {by_id[e['target']]['name']} both reference {e['properties'].get('phone')} [{evidence(e)}; candidate, verify source]" for e in matches]
        answer="Recorded shared-phone candidates: "+("; ".join(facts) if facts else "no shared phone association is stored for this case.")
        intent="shared_identifier"
    elif "transaction" in low or "transfer" in low or "recorded association" in low:
        rows=[e for e in edges if e["type"] in ("TRANSFERRED_TO","CALLED","ASSOCIATED_WITH","SHARED_PHONE")]
        facts=[f"{by_id[e['source']]['name']} —{e['type']}→ {by_id[e['target']]['name']} [{evidence(e)}]" for e in rows[:30]]
        answer="Recorded transaction/association records: "+("\n".join(facts) if facts else "none are present in the selected case.")
        intent="recorded_links"
    elif "summar" in low or "case" in low:
        answer=f"Case {case['case_name']} ({case_id}) contains {len(case.get('evidences_collected',[]))} stored documents, {len(nodes)-1} extracted entities, and {len(edges)} evidence-supported relationships. These are source records; no guilt or risk conclusion is inferred."
        facts=[d["name"] for d in case.get("evidences_collected",[])]; intent="case_summary"
    else:
        answer="I could not map that question to a supported local query. Try asking for connected entities, a path between two names, source documents for an entity, recorded transactions, case summary, or missing information."
        facts=[]; intent="unsupported"
    return {"answer":answer,"facts":facts,"intent":intent,"case_id":case_id,"source":"SQLite case, entity, relationship, and document records"}


def search(q, entity_type=None, case_id=None, limit=25):
    graph=graph_data(case_id)
    return [{"entity_id":n["id"],"display_name":n["name"],"labels":n["labels"],"case_ids":n["case_ids"],"properties":n["properties"]} for n in graph["nodes"] if (not entity_type or entity_type in n["labels"]) and q.lower() in (n["name"]+" "+_json(n["properties"])).lower()][:limit]


def entity_detail(entity_id):
    graph=graph_data()
    node=next((n for n in graph["nodes"] if n["id"] == entity_id),None)
    if not node: return None
    links=[e for e in graph["edges"] if entity_id in (e["source"],e["target"])]
    node["relationships"]=[{**e,"direction":"OUTGOING" if e["source"]==entity_id else "INCOMING","neighbor_id":e["target"] if e["source"]==entity_id else e["source"],"neighbor_name":next((n["name"] for n in graph["nodes"] if n["id"]==(e["target"] if e["source"]==entity_id else e["source"])),None)} for e in links]
    return node


def relationship_detail(relationship_id):
    with connect() as c: row=c.execute("SELECT * FROM relationships WHERE relationship_id=?",(relationship_id,)).fetchone()
    if row: require_case(row["case_id"])
    return None if not row else {"relationship_id":row["relationship_id"],"type":row["rel_type"],"start_node":row["source"],"end_node":row["target"],"properties":json.loads(row["properties"]),"evidence":row["evidence"],"certainty":row["certainty"]}


def rank(metric="degree", label="Person", limit=10, case_id=None):
    result=graph_data(case_id); graph=nx.Graph()
    graph.add_nodes_from(n["id"] for n in result["nodes"])
    graph.add_edges_from((e["source"],e["target"]) for e in result["edges"])
    if metric == "betweenness": scores=nx.betweenness_centrality(graph) if graph else {}
    elif metric == "pagerank": scores=nx.pagerank(graph) if graph.number_of_edges() else {n:0 for n in graph}
    else: scores=dict(graph.degree())
    by_id={n["id"]:n for n in result["nodes"]}
    selected=[(node,score) for node,score in scores.items() if (not label or label in by_id[node]["labels"]) and "Case" not in by_id[node]["labels"]]
    selected.sort(key=lambda x:(-x[1],x[0]))
    rows=[{"id":node,"name":by_id[node]["name"],"labels":by_id[node]["labels"],"score":float(score),"rank":idx,"case_ids":by_id[node]["case_ids"],"details":{"metric":metric}} for idx,(node,score) in enumerate(selected[:limit],1)]
    return {"metric":metric,"label":label,"supported":True,"total_ranked":len(rows),"rankings":rows}


def insights(case_id):
    graph=graph_data(case_id)
    return [{"insight_id":e["id"],"insight_type":"CROSS_DOMAIN_PATH" if e["type"] in ("CALLED","TRANSFERRED_TO") else "SHARED_ENTITY","title":f"Observed {e['type'].replace('_',' ').lower()} record","severity":"LOW","observed_facts":[f"{e['type']} link extracted from uploaded evidence."],"derived_interpretation":"This is a recorded evidence association, not a finding of wrongdoing; verify it against the source document.","alternative_explanations":["The recorded communication or transaction may have an ordinary explanation."],"disclaimer":"Candidate for human verification; no criminal allegation is inferred.","entities_involved":[e["source"],e["target"]],"case_ids":[case_id],"metadata":{"source_evidence":e["properties"].get("evidence"),"certainty":"observed"}} for e in graph["edges"] if e["type"] in ("CALLED","TRANSFERRED_TO")]


def local_dossier(case_id):
    case=case_detail(case_id)
    if not case: return None
    graph=graph_data(case_id)
    people=[n["name"] for n in graph["nodes"] if "Person" in n["labels"]]
    return {"case_id":case_id,"case_name":case["case_name"],"status":case.get("status","OPEN"),"risk_level":"NOT_ASSESSED","executive_summary":f"Evidence summary: {case['total_entities']} entities and {case['total_relationships']} links across {len(case['documents'])} documents.","modus_operandi":"Not assessed.","key_suspects":[p["name"] for p in case["primary_suspects"]],"victims":case["victims"],"witnesses":case["witnesses"],"persons_of_interest":case["persons_of_interest"],"role_conflicts":case["role_conflicts"],"critical_anomalies":[],"investigative_leads":[f"Verify extracted person record: {name}" for name in people],"key_findings":[f"{p['name']}: {', '.join(p['roles'])}" for p in case['people']],"ai_model":"local-rule-based-summary"}


def health():
    initialize()
    with connect() as c: c.execute("SELECT 1").fetchone()
    return {"status":"healthy","database":"sqlite","path":str(DB_PATH),"latency_ms":0}


def delete_case(case_id):
    with connect() as conn:
        paths=[r["storage_path"] for r in conn.execute("SELECT storage_path FROM documents WHERE case_id=?",(case_id,)).fetchall() if r["storage_path"]]
        node_count=conn.execute("SELECT count(*) FROM entities WHERE case_id=?",(case_id,)).fetchone()[0]
        rel_count=conn.execute("SELECT count(*) FROM relationships WHERE case_id=?",(case_id,)).fetchone()[0]
        cur=conn.execute("DELETE FROM cases WHERE case_id=?",(case_id,))
        if not cur.rowcount: return None
    for path in paths:
        try: Path(path).unlink(missing_ok=True)
        except OSError: pass
    return {"case_id":case_id,"nodes_removed":node_count,"nodes_detached":0,"relationships_removed":rel_count,"status":"deleted"}
