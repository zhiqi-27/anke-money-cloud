from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import time
import uuid
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from app.auth import AuthenticatedIdentity
from app.config import get_settings
from app.dependencies import (
    agent_access_service,
    cloud_service,
    current_agent,
    current_identity,
)
from app.models import (
    AgentAPIKeyCreated,
    AgentAPIKeyView,
    AgentAssetUpdate,
    AgentLedgerCreateResponse,
    AgentLedgerEntryCreate,
    AgentEntityCreateResponse,
    AgentEntityListResponse,
    AgentPrincipal,
    AuditListResponse,
    BootstrapResponse,
    DeviceRegistration,
    MigrationActivateRequest,
    MigrationResponse,
    MigrationUploadRequest,
    SyncPullResponse,
    SyncPushRequest,
    SyncPushResponse,
)
from app.services import (
    AgentAccessService,
    CloudService,
    WorkspaceNotActiveError,
)
from app.services.cloud import MembershipRequiredError
from app.mcp_server import mcp_asgi_app


logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def application_lifespan(_: FastAPI):
    async with mcp_asgi_app.router.lifespan_context(mcp_asgi_app):
        yield

fastapi_app = FastAPI(
    title="Anke Money Cloud API",
    description="Authorized synchronization and Agent Cloud boundary for Anke Money.",
    version="0.1.0",
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json",
    lifespan=application_lifespan,
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


@fastapi_app.exception_handler(RequestValidationError)
async def request_validation_failed(request: Request, exc: RequestValidationError):
    errors = [
        {
            "location": ".".join(str(part) for part in error["loc"]),
            "type": error["type"],
        }
        for error in exc.errors()
    ]
    logger.warning(
        "Request validation failed, path=%s errors=%s",
        request.url.path,
        errors,
    )
    return JSONResponse(
        status_code=422,
        content={"detail": "Request validation failed", "errors": errors},
    )


@fastapi_app.exception_handler(WorkspaceNotActiveError)
async def workspace_not_active(request: Request, exc: WorkspaceNotActiveError):
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": "Agent Cloud workspace is not active"},
    )


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


@fastapi_app.delete(
    "/api/v1/account",
    tags=["identity"],
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Permanently erase the owner account and Anke Cloud data",
)
async def delete_account(
    identity: AuthenticatedIdentity = Depends(current_identity),
    service: CloudService = Depends(cloud_service),
) -> Response:
    service.delete_account(identity)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@fastapi_app.post(
    "/api/v1/bootstrap",
    tags=["cloud"],
    response_model=BootstrapResponse,
    summary="Create or restore the owner household and register this device",
)
async def bootstrap(
    registration: DeviceRegistration,
    identity: AuthenticatedIdentity = Depends(current_identity),
    service: CloudService = Depends(cloud_service),
) -> BootstrapResponse:
    return service.bootstrap(identity, registration)


@fastapi_app.post(
    "/api/v1/agent-api-key",
    tags=["agent authorization"],
    response_model=AgentAPIKeyCreated,
    summary="Create or reset the full-capability Skill API key",
)
async def create_agent_api_key(
    identity: AuthenticatedIdentity = Depends(current_identity),
    service: CloudService = Depends(cloud_service),
    access: AgentAccessService = Depends(agent_access_service),
) -> AgentAPIKeyCreated:
    try:
        return service.create_agent_api_key(identity, access)
    except MembershipRequiredError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Bootstrap required") from exc


@fastapi_app.get(
    "/api/v1/agent-api-key",
    tags=["agent authorization"],
    response_model=AgentAPIKeyView | None,
    summary="Return the active Skill API key metadata",
)
async def agent_api_key(
    identity: AuthenticatedIdentity = Depends(current_identity),
    service: CloudService = Depends(cloud_service),
) -> AgentAPIKeyView | None:
    try:
        return service.agent_api_key(identity)
    except MembershipRequiredError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Bootstrap required") from exc


@fastapi_app.delete(
    "/api/v1/agent-api-key",
    tags=["agent authorization"],
    response_model=AgentAPIKeyView | None,
    summary="Revoke the active Skill API key immediately",
)
async def revoke_agent_api_key(
    identity: AuthenticatedIdentity = Depends(current_identity),
    service: CloudService = Depends(cloud_service),
) -> AgentAPIKeyView | None:
    try:
        return service.revoke_agent_api_key(identity)
    except MembershipRequiredError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Bootstrap required") from exc


@fastapi_app.post(
    "/agent/v1/ledger/entries",
    tags=["agent"],
    response_model=AgentLedgerCreateResponse,
    summary="Append an idempotent ledger entry with agent scope",
)
async def agent_create_ledger_entry(
    request: AgentLedgerEntryCreate,
    principal: AgentPrincipal = Depends(current_agent),
    service: CloudService = Depends(cloud_service),
) -> AgentLedgerCreateResponse:
    try:
        return service.agent_create_ledger_entry(principal, request)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient agent scope") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@fastapi_app.get(
    "/agent/v1/ledger/entries",
    tags=["agent"],
    response_model=AgentEntityListResponse,
    summary="Read ledger entries with agent scope",
)
async def agent_list_ledger_entries(
    limit: int = Query(default=200, ge=1, le=500),
    principal: AgentPrincipal = Depends(current_agent),
    service: CloudService = Depends(cloud_service),
) -> AgentEntityListResponse:
    try:
        return service.agent_list_ledger_entries(principal, limit)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient agent scope") from exc


@fastapi_app.get(
    "/agent/v1/assets",
    tags=["agent"],
    response_model=AgentEntityListResponse,
    summary="Read asset accounts and snapshots with agent scope",
)
async def agent_list_assets(
    limit: int = Query(default=200, ge=1, le=500),
    principal: AgentPrincipal = Depends(current_agent),
    service: CloudService = Depends(cloud_service),
) -> AgentEntityListResponse:
    try:
        return service.agent_list_assets(principal, limit)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient agent scope") from exc


@fastapi_app.patch(
    "/agent/v1/assets/{account_id}",
    tags=["agent"],
    response_model=AgentEntityCreateResponse,
    summary="Update one asset by appending an idempotent snapshot",
)
async def agent_update_asset(
    account_id: UUID,
    request: AgentAssetUpdate,
    principal: AgentPrincipal = Depends(current_agent),
    service: CloudService = Depends(cloud_service),
) -> AgentEntityCreateResponse:
    try:
        return service.agent_update_asset(principal, account_id, request)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient agent scope") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@fastapi_app.get(
    "/agent/v1/categories",
    tags=["agent"],
    response_model=AgentEntityListResponse,
    summary="Read categories with agent scope",
)
async def agent_list_categories(
    limit: int = Query(default=200, ge=1, le=500),
    principal: AgentPrincipal = Depends(current_agent),
    service: CloudService = Depends(cloud_service),
) -> AgentEntityListResponse:
    try:
        return service.agent_list_categories(principal, limit)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient agent scope") from exc


@fastapi_app.get(
    "/agent/v1/channels",
    tags=["agent"],
    response_model=AgentEntityListResponse,
    summary="Read payment channels with agent scope",
)
async def agent_list_channels(
    limit: int = Query(default=200, ge=1, le=500),
    principal: AgentPrincipal = Depends(current_agent),
    service: CloudService = Depends(cloud_service),
) -> AgentEntityListResponse:
    try:
        return service.agent_list_channels(principal, limit)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient agent scope") from exc


@fastapi_app.post(
    "/api/v1/sync/push",
    tags=["sync"],
    response_model=SyncPushResponse,
    summary="Push an ordered device mutation batch",
)
async def sync_push(
    request: SyncPushRequest,
    identity: AuthenticatedIdentity = Depends(current_identity),
    service: CloudService = Depends(cloud_service),
) -> SyncPushResponse:
    try:
        return service.push(identity, request)
    except MembershipRequiredError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Bootstrap required") from exc


@fastapi_app.get(
    "/api/v1/sync/pull",
    tags=["sync"],
    response_model=SyncPullResponse,
    summary="Pull household changes after an opaque cursor",
)
async def sync_pull(
    cursor: str | None = Query(default=None, max_length=2048),
    limit: int = Query(default=200, ge=1, le=500),
    identity: AuthenticatedIdentity = Depends(current_identity),
    service: CloudService = Depends(cloud_service),
) -> SyncPullResponse:
    try:
        return service.pull(identity, cursor, limit)
    except MembershipRequiredError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Bootstrap required") from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid sync cursor") from exc


@fastapi_app.get(
    "/api/v1/audit",
    tags=["audit"],
    response_model=AuditListResponse,
    summary="List redacted remote-operation audit events for the owner",
)
async def audit_events(
    cursor: str | None = Query(default=None, max_length=2048),
    limit: int = Query(default=100, ge=1, le=200),
    identity: AuthenticatedIdentity = Depends(current_identity),
    service: CloudService = Depends(cloud_service),
) -> AuditListResponse:
    try:
        return service.audit(identity, cursor, limit)
    except MembershipRequiredError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Bootstrap required") from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid audit cursor") from exc


@fastapi_app.post(
    "/api/v1/migrations",
    tags=["migration"],
    response_model=MigrationResponse,
    summary="Stage an idempotent Local snapshot migration",
)
async def stage_migration(
    request: MigrationUploadRequest,
    identity: AuthenticatedIdentity = Depends(current_identity),
    service: CloudService = Depends(cloud_service),
) -> MigrationResponse:
    try:
        return service.stage_migration(identity, request)
    except MembershipRequiredError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Bootstrap required") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@fastapi_app.post(
    "/api/v1/migrations/activate",
    tags=["migration"],
    response_model=MigrationResponse,
    summary="Activate a verified staged migration",
)
async def activate_migration(
    request: MigrationActivateRequest,
    identity: AuthenticatedIdentity = Depends(current_identity),
    service: CloudService = Depends(cloud_service),
) -> MigrationResponse:
    try:
        return service.activate_migration(identity, request.session_id, request.content_digest)
    except MembershipRequiredError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Bootstrap required") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


# Keep the protocol endpoint outside OpenAPI while sharing this process and service layer.
fastapi_app.mount("", mcp_asgi_app, name="anke-money-mcp")
