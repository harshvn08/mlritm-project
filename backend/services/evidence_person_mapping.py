"""Read explicit person/evidence mappings without inferring investigative roles."""
import csv
import hashlib
import io
import json
from pathlib import Path


def apply_person_mappings(case_id, documents, nodes):
    edges = []
    records = []
    for filename, digest, text, *_ in documents:
        if Path(filename).suffix.lower() != ".csv":
            continue
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
        headers = {str(key or "").strip().lower() for key in reader.fieldnames or []}
        if not {"entity_name", "entity_type", "relationship", "related_entity"}.issubset(headers):
            continue
        document_id = hashlib.sha256(f"{case_id}:{digest}".encode()).hexdigest()
        for index, raw in enumerate(reader, 2):
            row = {str(key or "").strip().lower(): str(value or "").strip() for key, value in raw.items()}
            if row["entity_type"].casefold() != "person" or not row["entity_name"]:
                continue
            records.append((filename, document_id, index, row))

    def find(label, value):
        return next((node for node in nodes.values() if label in node["labels"] and node["name"].casefold() == value.casefold()), None)

    def ensure(label, value, properties=None):
        node = find(label, value)
        if node is None:
            entity_id = label.upper() + "_" + hashlib.sha256(f"{case_id}:{value.casefold()}".encode()).hexdigest()[:20]
            node = {"id": entity_id, "labels": [label], "name": value, "case_ids": [case_id],
                    "properties": {"entity_type": label.upper(), "canonical_name": value, **(properties or {})}}
            nodes[entity_id] = node
        return node

    # All source people are declared before resolving person-to-person links.
    for _, _, _, row in records:
        ensure("Person", row["entity_name"])

    for filename, document_id, index, row in records:
        source = find("Person", row["entity_name"])
        props = source["properties"]
        props.setdefault("mapping_records", []).append({**row, "mapping_file": filename, "source_document_id": document_id, "source_record": index})
        relation = row["relationship"].lower()
        target_value = row["related_entity"]
        target = None
        if relation == "uses_phone":
            value = target_value.removeprefix("PHONE-")
            target = ensure("Phone", value, {"phone_number": value}) if value else None
        elif relation == "associated_with_vehicle":
            value = target_value.removeprefix("VEHICLE-")
            target = ensure("Vehicle", value, {"license_plate": value}) if value else None
        elif relation == "gave_statement":
            target = ensure("Document", target_value, {"statement_id": target_value, "source_document": row.get("source_file")}) if target_value else None
        elif relation != "victim_in_case":
            target = find("Person", target_value)
        if target is None:
            # Case membership is metadata, not a fabricated edge to an external
            # case identifier. Unresolved rows remain inspectable on the person.
            continue
        source_file = row.get("source_file") or filename
        evidence_document = next((hashlib.sha256(f"{case_id}:{digest}".encode()).hexdigest()
                                  for name, digest, *_ in documents if name == source_file), document_id)
        reference = row.get("record_reference") or row.get("mapping_id") or f"row {index}"
        try:
            confidence = float(row.get("confidence", ""))
            confidence = confidence if 0 <= confidence <= 1 else None
        except ValueError:
            confidence = None
        status = row.get("status") or "source_assertion"
        evidence = f"{filename} · {row.get('mapping_id') or index} · {source_file} · {reference}"
        edge_properties = {"source_document": source_file, "source_register": filename, "source_record": index,
                           "mapping_id": row.get("mapping_id"), "record_reference": reference,
                           "evidence_document_id": evidence_document, "source_register_document_id": document_id,
                           "evidence_text": json.dumps(row, ensure_ascii=False), "verification_status": status,
                           "confidence": confidence}
        edges.append({"source": source["id"], "target": target["id"], "type": relation.upper(),
                      "properties": edge_properties, "evidence": evidence,
                      "certainty": "candidate" if "lead" in status.lower() else "source_assertion"})
    return edges
