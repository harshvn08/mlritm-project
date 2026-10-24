from typing import Optional, Union
from fastapi import APIRouter, HTTPException, Query, status
from neo4j.exceptions import ServiceUnavailable

from backend.database import db
from backend.models.path import ShortestPathResponse, AmbiguousPathResponse
from backend.services.path_service import PathService
from backend.config import settings
from backend import local_store
import networkx as nx
from backend.logging_config import logger

router = APIRouter(tags=["Shortest Path Analysis"])


def _handle_shortest_path(
    suspect_name: Optional[str],
    victim_name: Optional[str],
    suspect_id: Optional[str],
    victim_id: Optional[str],
    max_depth: int
) -> Union[ShortestPathResponse, AmbiguousPathResponse]:
    if settings.STORAGE_BACKEND.lower() == "sqlite":
        graph_data=local_store.graph_data()
        by_id={n["id"]:n for n in graph_data["nodes"]}
        graph=nx.Graph()
        graph.add_edges_from((e["source"],e["target"],{"id":e["id"],"type":e["type"],**e}) for e in graph_data["edges"])
        def find(value):
            return next((n for n in graph_data["nodes"] if (value == n["id"] or value.lower() == n["name"].lower()) and "Person" in n["labels"]),None)
        start=find(suspect_id or suspect_name or ""); end=find(victim_id or victim_name or "")
        if not start or not end: return ShortestPathResponse(path_length=0,summary="Person not found in locally stored evidence.")
        try: path=nx.shortest_path(graph,start["id"],end["id"])
        except (nx.NetworkXNoPath,nx.NodeNotFound): return ShortestPathResponse(path_length=0,summary="No evidence-linked path found.")
        nodes=[by_id[n] for n in path]; rels=[]
        for a,b in zip(path,path[1:]):
            edge=next((e for e in graph_data["edges"] if {e["source"],e["target"]}=={a,b}),None)
            if edge: rels.append({"type":edge["type"],"source":a,"target":b,"properties":edge["properties"]})
        return ShortestPathResponse(nodes=nodes,relationships=rels,path_length=len(rels),summary="Path shows evidence associations and requires analyst verification.")
    if not (suspect_name or suspect_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must provide either 'suspect_name' or 'suspect_id'."
        )
    if not (victim_name or victim_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must provide either 'victim_name' or 'victim_id'."
        )

    try:
        with db.get_session() as session:
            result = PathService.find_shortest_path(
                session=session,
                suspect_name=suspect_name,
                victim_name=victim_name,
                suspect_id=suspect_id,
                victim_id=victim_id,
                max_depth=max_depth
            )

            # If ambiguous, return the AmbiguousPathResponse
            if getattr(result, "ambiguous", False):
                return result

            # If person wasn't found at all
            if result.path_length == 0 and "not found" in (result.summary or ""):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=result.summary
                )

            return result
    except ServiceUnavailable as se:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(se))


@router.get("/api/cases/shortest-path", response_model=Union[ShortestPathResponse, AmbiguousPathResponse], summary="Find Shortest Path between Suspect and Victim")
def get_cases_shortest_path(
    suspect_name: Optional[str] = Query(None, description="Suspect full name"),
    victim_name: Optional[str] = Query(None, description="Victim full name"),
    suspect_id: Optional[str] = Query(None, description="Suspect person_id"),
    victim_id: Optional[str] = Query(None, description="Victim person_id"),
    max_depth: int = Query(5, ge=1, le=15, description="Maximum hops to traverse")
):
    return _handle_shortest_path(suspect_name, victim_name, suspect_id, victim_id, max_depth)


@router.get("/api/graph/shortest-path", response_model=Union[ShortestPathResponse, AmbiguousPathResponse], summary="Alias: Shortest Path Traversal")
def get_graph_shortest_path(
    suspect_name: Optional[str] = Query(None, description="Suspect full name"),
    victim_name: Optional[str] = Query(None, description="Victim full name"),
    suspect_id: Optional[str] = Query(None, description="Suspect person_id"),
    victim_id: Optional[str] = Query(None, description="Victim person_id"),
    max_depth: int = Query(5, ge=1, le=15, description="Maximum hops to traverse")
):
    return _handle_shortest_path(suspect_name, victim_name, suspect_id, victim_id, max_depth)

