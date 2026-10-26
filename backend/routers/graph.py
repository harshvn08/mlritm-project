from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Query, status, Request
from neo4j.exceptions import ServiceUnavailable

from backend.database import db
from backend.services.graph_service import GraphService
from backend.logging_config import logger
from backend.config import settings
from backend import local_store

router = APIRouter(tags=["Graph & Entity Operations"])


@router.get("/api/graph", summary="Retrieve Graph Subgraph for Visualization")
def get_graph(
    case_id: Optional[str] = Query(None, description="Filter by case ID"),
    node_labels: Optional[List[str]] = Query(None, description="Filter by node labels"),
    core_only: bool = Query(False, description="Restrict to core target suspects and owned assets"),
    graph_type: str = Query("all", description="Filter graph by extraction type: all, cdr, person, financial"),
    limit: int = Query(1000, ge=1, le=5000, description="Max elements to fetch"),
    focus_entity_id: Optional[str] = Query(None, description="Return a one- or two-hop neighborhood"),
    hops: int = Query(1, ge=1, le=2)
):
    if settings.STORAGE_BACKEND.lower() in {"sqlite", "postgres"}:
        result = local_store.graph_data(case_id, limit=None if focus_entity_id else limit, focus_entity_id=focus_entity_id, hops=hops)
        if node_labels:
            result["nodes"] = [n for n in result["nodes"] if any(label in n["labels"] for label in node_labels)]
            ids = {n["id"] for n in result["nodes"]}
            result["edges"] = [e for e in result["edges"] if e["source"] in ids and e["target"] in ids]
        if graph_type == "cdr":
            wanted={"Person","Phone","CellTower"}
            result["nodes"]=[n for n in result["nodes"] if wanted.intersection(n["labels"])]
            ids={n["id"] for n in result["nodes"]}
            result["edges"]=[e for e in result["edges"] if e["source"] in ids and e["target"] in ids and e["type"] in {"CALLED","ASSOCIATED_WITH","SHARED_PHONE"}]
        elif graph_type == "financial":
            wanted={"Person","BankAccount","Organization"}
            result["nodes"]=[n for n in result["nodes"] if wanted.intersection(n["labels"])]
            ids={n["id"] for n in result["nodes"]}
            result["edges"]=[e for e in result["edges"] if e["source"] in ids and e["target"] in ids and e["type"] in {"TRANSFERRED_TO","ASSOCIATED_WITH"}]
        elif graph_type == "person":
            result["nodes"]=[n for n in result["nodes"] if str(n.get("entity_type", "")).upper() == "PERSON" or "Person" in n["labels"]]
            ids={n["id"] for n in result["nodes"]}
            result["edges"]=[e for e in result["edges"] if e["source"] in ids and e["target"] in ids]
        if core_only:
            connected = {x for e in result["edges"] for x in (e["source"], e["target"])}
            result["nodes"] = [n for n in result["nodes"] if n["id"] in connected]
        result["total_nodes"], result["total_edges"] = len(result["nodes"]), len(result["edges"])
        return result
    try:
        with db.get_session() as session:
            return GraphService.get_subgraph(
                session,
                case_id=case_id,
                node_labels=node_labels,
                core_only=core_only,
                graph_type=graph_type,
                limit=limit
            )
    except ServiceUnavailable as se:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(se))




@router.get("/api/entities/search", summary="Search Entities Across Graph")
def search_entities(
    q: str = Query(..., min_length=1, description="Search query string (name, phone, account, VIN, etc.)"),
    entity_type: Optional[str] = Query(None, description="Optional entity label filter (Person, Phone, BankAccount, etc.)"),
    case_id: Optional[str] = Query(None, description="Optional case ID filter"),
    limit: int = Query(25, ge=1, le=100, description="Max search results")
):
    if settings.STORAGE_BACKEND.lower() in {"sqlite", "postgres"}:
        return local_store.search(q, entity_type, case_id, limit)
    try:
        with db.get_session() as session:
            return GraphService.search_entities(
                session=session,
                query=q,
                entity_type=entity_type,
                case_id=case_id,
                limit=limit
            )
    except ServiceUnavailable as se:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(se))


@router.get("/api/entities/{entity_id}", summary="Entity 360 Profile & 1-Hop Neighbors")
def get_entity(entity_id: str):
    if settings.STORAGE_BACKEND.lower() in {"sqlite", "postgres"}:
        entity = local_store.entity_detail(entity_id)
        if not entity: raise HTTPException(status_code=404, detail=f"Entity '{entity_id}' not found.")
        return entity
    try:
        with db.get_session() as session:
            entity_data = GraphService.get_entity_360(session, entity_id)
            if not entity_data:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Entity with ID '{entity_id}' not found in graph."
                )
            return entity_data
    except ServiceUnavailable as se:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(se))


@router.get("/api/relationships/{relationship_id}", summary="Get Relationship Details")
def get_relationship(relationship_id: str):
    if settings.STORAGE_BACKEND.lower() in {"sqlite", "postgres"}:
        relationship = local_store.relationship_detail(relationship_id)
        if not relationship: raise HTTPException(status_code=404, detail=f"Relationship '{relationship_id}' not found.")
        return relationship
    try:
        with db.get_session() as session:
            rel_data = GraphService.get_relationship_by_id(session, relationship_id)
            if not rel_data:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Relationship with ID '{relationship_id}' not found."
                )
            return rel_data
    except ServiceUnavailable as se:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(se))


from backend.models.copilot import CopilotRequest


@router.post("/api/graph/ai-query", summary="Authenticated free-form Gemini Copilot conversation")
def ask_graph_copilot(payload: CopilotRequest, request: Request):
    from backend.services.gemini_service import GeminiService
    from backend.services.copilot_context import build_copilot_context
    from backend.case_access import current_officer, resolve_session
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Question is required.")
    officer = current_officer.get()
    # Neo4j routes do not use the SQLite middleware context, but Copilot still
    # requires the same server-issued login session and explicit ownership check.
    if not officer:
        authorization = request.headers.get("authorization", "")
        officer = resolve_session(authorization[7:]) if authorization.startswith("Bearer ") else None
    if not officer:
        raise HTTPException(status_code=401, detail="Sign in to use the Gemini Copilot.")
    case_id = (payload.case_id or "").strip() or None
    context = build_copilot_context(case_id, officer, question, payload.selected_node_id)
    return GeminiService.chat(question, context, payload.history)


