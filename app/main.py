from __future__ import annotations

import logging
import time
import uuid

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from app.auth import AuthenticatedIdentity
from app.config import get_settings
from app.dependencies import current_identity


logger = logging.getLogger(__name__)
settings = get_settings()

fastapi_app = FastAPI(
    title="Anke Money Cloud API",
    description="Authorized synchronization and Agent Cloud boundary for Anke Money.",
    version="0.1.0",
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json",
)


@fastapi_app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "Request failed, request_id=%s method=%s path=%s",
            request_id,
            request.method,
            request.url.path,
        )
        raise
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "Request completed, request_id=%s method=%s path=%s status=%s duration_ms=%s",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


@fastapi_app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    logger.error(
        "Unhandled error, path=%s error_type=%s",
        request.url.path,
        type(exc).__name__,
    )
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@fastapi_app.get("/ping", tags=["health"], summary="Process health")
async def ping() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "anke-money-cloud",
        "environment": settings.environment,
    }


@fastapi_app.get(
    "/api/v1/me",
    tags=["identity"],
    summary="Return the verified Firebase identity",
    responses={
        401: {"description": "Missing or invalid Firebase ID token"},
        503: {"description": "Authentication service is not configured"},
    },
)
async def me(
    identity: AuthenticatedIdentity = Depends(current_identity),
) -> dict[str, str]:
    return {"uid": identity.uid}
