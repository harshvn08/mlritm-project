"""Source-supported person roles, independent of graph connectivity."""
import csv
import io
import re
import json

ROLE_PATTERN = r"(?:person[ _]of[ _]interest|suspect(?:\s+[AB])?|accused|victim|complainant|witness|unknown)"


def normalize_role(value):
    value = re.sub(r"[ _-]+", " ", str(value or "").strip().lower())
    if value.startswith("suspect") or value == "accused":
        return "suspect"
    return {"victim": "victim", "complainant": "victim", "witness": "witness",
            "person of interest": "person_of_interest"}.get(value, "unknown")


def extract_roles(documents):
    assertions = []
    for filename, text in documents:
        if filename.lower().endswith(".json"):
            try:
                payload = json.loads(text)
                if isinstance(payload, dict):
                    payload = payload.get("case_data", payload)
                    for index, person in enumerate(payload.get("entities", {}).get("people", payload.get("people", [])), 1):
                        roles = person.get("roles") or [person.get("role") or person.get("status")]
                        if isinstance(roles, str): roles = [roles]
                        for role in roles:
                            if person.get("name") and normalize_role(role) != "unknown":
                                assertions.append((person["name"], normalize_role(role), filename, index, json.dumps(person)))
            except (ValueError, TypeError, AttributeError):
                pass
        if filename.lower().endswith(".csv"):
            reader = csv.DictReader(io.StringIO(text))
            for index, raw in enumerate(reader, 2):
                row = {str(k or "").lower().strip().replace(" ", "_"): v for k, v in raw.items()}
                name = row.get("person_name") or row.get("entity_name") or row.get("name")
                role = row.get("role") or row.get("person_role") or row.get("classification") or row.get("status")
                if name and role and normalize_role(role) != "unknown":
                    assertions.append((name.strip(), normalize_role(role), filename, index, str(raw)))
                # This is an explicit source role, unlike uses_phone or dispute_with,
                # which do not establish a person's investigative classification.
                if name and str(row.get("relationship") or "").strip().lower() == "victim_in_case":
                    assertions.append((name.strip(), "victim", filename, index, str(raw)))
        for index, line in enumerate(text.splitlines(), 1):
            heading = re.match(
                r"^\s*(?:STATEMENT|INTERVIEW)\s+[\w-]+\s*[—–:-]\s*([^()\r\n]+?)\s*\(([^)]+)\)\s*$",
                line, re.I,
            )
            if heading:
                name, labels = heading.groups()
                name = name.strip().title() if name.strip().isupper() else name.strip()
                for label in re.split(r"\s*[/;,|]\s*", labels):
                    role = normalize_role(label)
                    if role != "unknown":
                        assertions.append((name, role, filename, index, line.strip()))
            patterns = [
                rf"^\s*(?:[-*]\s*)?({ROLE_PATTERN})\s*[:—–-]\s*([A-Z][\w'.-]+(?:[ \t]+[A-Z][\w'.-]+){{1,3}})",
                rf"^\s*(?:[-*]\s*)?([A-Z][\w'.-]+(?:[ \t]+[A-Z][\w'.-]+){{1,3}})\s*(?:[—–:,-]|\bis\b)\s*({ROLE_PATTERN})\b",
            ]
            for i, pattern in enumerate(patterns):
                match = re.search(pattern, line, re.I)
                if match:
                    role, name = match.groups() if i == 0 else tuple(reversed(match.groups()))
                    assertions.append((name.strip(), normalize_role(role), filename, index, line.strip()))
    return assertions


def role_properties(assertions):
    evidence = [dict(role=role, source_document=filename, source_record=line, evidence_text=text,
                     extraction_method="explicit_role") for _, role, filename, line, text in assertions]
    roles = sorted({item["role"] for item in evidence} - {"unknown"}) or ["unknown"]
    # A source can explicitly call a suspect a person of interest too. Those
    # labels are compatible; victim/suspect/witness disagreements need review.
    conflict = len(set(roles) & {"victim", "suspect", "witness"}) > 1
    primary = next((role for role in ("suspect", "victim", "witness", "person_of_interest") if role in roles), "unknown")
    return {"roles": roles, "role": "unknown" if conflict else primary,
            "role_conflict": conflict, "role_provenance": evidence, "role_version": 2}
