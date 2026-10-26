"""Build bounded Copilot context exclusively from authorized persisted records."""
import json
import re
from fastapi import HTTPException
from backend import local_store
from backend.config import settings
from backend.database import db
from backend.services.graph_service import GraphService

MAX_CASES = 5
MAX_NODES = 200
MAX_EDGES = 250
MAX_DOCUMENTS = 20


def compact(value, depth=0):
    if depth > 6:
        return "[nested data omitted]"
    if isinstance(value, str):
        return value[:1200]
    if isinstance(value, dict):
        return {str(k): compact(v, depth + 1) for k, v in list(value.items())[:35] if k not in {"content", "accessToken", "password", "passwordHash"}}
    if isinstance(value, (list, tuple)):
        return [compact(v, depth + 1) for v in value[:12]]
    return value


def document_excerpt(text, question):
    terms = set(re.findall(r"\w{3,}", question.lower()))
    lines = [(index, line) for index, line in enumerate(text.splitlines(), 1) if line.strip()]
    ranked = sorted(lines, key=lambda item: -sum(term in item[1].lower() for term in terms))[:6]
    return [{"line": index, "text": line[:400]} for index, line in sorted(ranked)]


def build_copilot_context(case_id, officer, question, selected_node_id=None):
    if not officer:
        raise HTTPException(status_code=401, detail="Sign in to use the Gemini Copilot.")
    result = {"scope": "selected_case" if case_id else "authorized_cases", "cases": [], "limits": {
        "cases": MAX_CASES, "entities_per_case": MAX_NODES, "relationships_per_case": MAX_EDGES,
        "documents_per_case": MAX_DOCUMENTS, "note": "Context is a bounded excerpt, not the full evidence archive."}}
    if settings.STORAGE_BACKEND.lower() == "sqlite":
        if case_id:
            from backend.case_access import require_case
            require_case(case_id)
            with local_store.connect() as connection:
                row = connection.execute("SELECT case_id,title,metadata FROM cases WHERE case_id=?", (case_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"Case '{case_id}' not found.")
            summary = {**json.loads(row["metadata"]), "case_id": row["case_id"], "case_name": row["title"]}
            cases = [summary]
        else:
            from backend.case_access import may_read
            # Authorize before pagination so another officer's recent cases do
            # not hide this officer's older records or enter the model context.
            cases = []
            with local_store.connect() as connection:
                for row in connection.execute("SELECT case_id,title,metadata FROM cases ORDER BY created_at DESC,case_id"):
                    metadata = json.loads(row["metadata"])
                    if may_read(metadata):
                        cases.append({**metadata, "case_id": row["case_id"], "case_name": row["title"]})
                        if len(cases) == MAX_CASES: break
        for summary in cases:
            cid = summary["case_id"]
            graph = local_store.graph_data(cid, limit=MAX_NODES)
            with local_store.connect() as connection:
                counts = {"entities": connection.execute("SELECT count(*) FROM entities WHERE case_id=? AND label!='Case'", (cid,)).fetchone()[0],
                          "relationships": connection.execute("SELECT count(*) FROM relationships WHERE case_id=?", (cid,)).fetchone()[0],
                          "documents": connection.execute("SELECT count(*) FROM documents WHERE case_id=?", (cid,)).fetchone()[0]}
                documents = connection.execute("SELECT document_id,filename,sha256,substr(content,1,50000) AS content,length(content) AS original_chars FROM documents WHERE case_id=? ORDER BY created_at,document_id LIMIT ?", (cid, MAX_DOCUMENTS)).fetchall()
            # Actual graph totals/roles come from storage, never browser counts.
            result["cases"].append({
                "case": {k: summary.get(k) for k in ("case_id", "case_name", "fir_number", "status", "case_type", "summary")},
                "analysis": counts,
                "entities": [compact(n) for n in graph["nodes"][:MAX_NODES]],
                "relationships": [compact(e) for e in graph["edges"][:MAX_EDGES]],
                "documents": [{"document_id": d["document_id"], "filename": d["filename"], "sha256": d["sha256"],
                               "excerpt_scan_limited": d["original_chars"] > 50000,
                               "excerpts": document_excerpt(d["content"], question)} for d in documents],
                "truncated": bool(graph.get("limited")) or counts["entities"] > MAX_NODES or counts["relationships"] > MAX_EDGES or counts["documents"] > MAX_DOCUMENTS or any(d["original_chars"] > 50000 for d in documents),
            })
    else:
        with db.get_session() as session:
            cases = GraphService.list_cases(session, uploaded_by=officer, limit=MAX_CASES) if not case_id else [{"case_id": case_id}]
            for summary in cases:
                cid = summary["case_id"]
                row = session.run("MATCH (c:Case {case_id:$case_id}) RETURN properties(c) AS metadata", {"case_id": cid}).single()
                if not row:
                    if case_id: raise HTTPException(status_code=404, detail=f"Case '{cid}' not found.")
                    continue
                metadata = row["metadata"]
                owner = metadata.get("uploaded_by")
                if (owner and str(owner).casefold() != officer.casefold()) or (not owner and settings.APP_ENV == "production"):
                    if case_id: raise HTTPException(status_code=403, detail="You do not have access to this case.")
                    continue
                graph = GraphService.get_subgraph(session, case_id=cid, limit=MAX_NODES)
                result["cases"].append({"case": compact(metadata), "entities": [compact(n) for n in graph.get("nodes", [])[:MAX_NODES]],
                                        "relationships": [compact(e) for e in graph.get("edges", [])[:MAX_EDGES]], "truncated": True})
    # Keep the entire JSON valid when capping context. Do not cut serialized JSON.
    while len(json.dumps(result, default=str)) > 100000:
        largest = max(result["cases"], key=lambda case: len(json.dumps(case, default=str)))
        records = max((largest.get(key, []) for key in ("entities", "relationships", "documents")), key=len)
        if not records: break
        records.pop()
        largest["truncated"] = True
    if selected_node_id and any(n.get("id") == selected_node_id for case in result["cases"] for n in case.get("entities", [])):
        result["selected_node_id"] = selected_node_id
    return result
