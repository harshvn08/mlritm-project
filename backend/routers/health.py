from fastapi import APIRouter, HTTPException, status, Response
from backend.database import db
from backend.logging_config import logger
from backend.config import settings
from backend.local_store import health as local_health

router = APIRouter(prefix="/api", tags=["Health & System"])


@router.get("/health", summary="Health Check & Neo4j Connectivity")
def health_check():
    if settings.STORAGE_BACKEND.lower() == "sqlite":
        return local_health()
    health = db.check_health()
    if health["status"] != "healthy":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=health
        )
    return health


@router.head("/health", include_in_schema=False)
def health_check_head():
    if settings.STORAGE_BACKEND.lower() == "sqlite":
        local_health()
        return Response(status_code=status.HTTP_200_OK)
    health = db.check_health()
    if health["status"] != "healthy":
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response(status_code=status.HTTP_200_OK)

