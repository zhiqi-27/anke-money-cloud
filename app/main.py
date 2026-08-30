from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime
import logging
import time
import uuid
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from app.auth import AuthenticatedIdentity, InvalidClerkCredentialError
from app.config import ConfigurationError, get_settings
from app.dependencies import (
    agent_access_service,
    auth_service,
    cloud_service,
    current_agent,
    current_identity,
    current_admin,
    clerk_bearer,
    clerk_management_service,
    billing_service,
    admin_service,
)
from app.models import (
    AgentAPIKeyCreated,
    AgentAPIKeyView,
    AgentAssetBatchCreate,
    AgentAssetBatchCreateResponse,
    AgentAssetCreate,
    AgentAssetCreateResponse,
    AgentAssetUpdate,
    AgentLedgerBatchCreate,
    AgentLedgerBatchCreateResponse,
    AgentLedgerCreateResponse,
    AgentLedgerEntryCreate,
    AgentEntityCreateResponse,
    AgentEntityListResponse,
    AgentPrincipal,
    AuditListResponse,
    BootstrapResponse,
    DeviceRegistration,
    PushTokenRegistration,
    MigrationActivateRequest,
    MigrationResponse,
    MigrationUploadRequest,
    SyncPullResponse,
    SyncPushRequest,
    SyncPushResponse,
    AnkeSessionResponse,
    ProfileUpdateRequest,
    AppleEntitlementVerificationRequest,
    AppleServerNotificationRequest,
    ProEntitlementView,
    AdminAuditListResponse,
    AdminGrantCreateRequest,
    AdminGrantMutationResponse,
    AdminGrantRevokeRequest,
    AdminManualGrantListResponse,
    AdminOverviewResponse,
    AdminUserDetail,
    AdminUserEntitlementResponse,
    AdminUserListResponse,
    AdminUserStatus,
)
from app.services import (
    AgentAccessService,
    CloudService,
    AuthService,
    ClerkManagementClient,
    ClerkManagementError,
    WorkspaceNotActiveError,
    AppleBillingService,
    AppleTransactionAlreadyLinkedError,
    InvalidAppleTransactionError,
    ProEntitlementRequiredError,
    AdminGrantAlreadyRevokedError,
    AdminGrantNotFoundError,
    AdminIdempotencyConflictError,
    AdminInvalidGrantPeriodError,
    AdminService,
    AdminTargetNotFoundError,
    AdminTargetNotReadyError,
)
from app.services.cloud import DeviceRegistrationRequiredError, MembershipRequiredError
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


@fastapi_app.exception_handler(ProEntitlementRequiredError)
async def pro_entitlement_required(request: Request, exc: ProEntitlementRequiredError):
    return JSONResponse(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        content={"detail": "An active Anke Pro subscription is required"},
    )


@fastapi_app.get("/ping", tags=["health"], summary="Process health")
async def ping() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "anke-money-cloud",
        "environment": settings.environment,
    }


@fastapi_app.post(
    "/api/v1/auth/clerk/exchange",
    tags=["identity"],
    response_model=AnkeSessionResponse,
    summary="Verify a Clerk session and issue an Anke session",
    responses={
        401: {"description": "Invalid Clerk session"},
        503: {"description": "Authentication service is not configured"},
    },
)
async def authenticate_with_clerk(
    credentials: HTTPAuthorizationCredentials | None = Security(clerk_bearer),
    service: AuthService = Depends(auth_service),
) -> AnkeSessionResponse:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing Clerk authentication",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return service.sign_in_with_clerk(
            f"{credentials.scheme} {credentials.credentials}"
        )
    except InvalidClerkCredentialError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Clerk authentication failed",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except ConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service unavailable",
        ) from exc


@fastapi_app.get(
    "/api/v1/me",
    tags=["identity"],
    summary="Return the verified Anke identity",
    responses={
        401: {"description": "Missing or invalid Anke session token"},
        503: {"description": "Authentication service is not configured"},
    },
)
async def me(
    identity: AuthenticatedIdentity = Depends(current_identity),
) -> dict[str, str | None]:
    return {
        "uid": identity.uid,
        "provider": identity.provider,
        "displayName": identity.display_name,
        "email": identity.email,
    }


@fastapi_app.post(
    "/api/v1/billing/apple/verify",
    tags=["billing"],
    response_model=ProEntitlementView,
    summary="Verify an Apple transaction and bind Anke Pro to this account",
)
async def verify_apple_entitlement(
    request: AppleEntitlementVerificationRequest,
    identity: AuthenticatedIdentity = Depends(current_identity),
    service: AppleBillingService = Depends(billing_service),
) -> ProEntitlementView:
    try:
        return service.verify_and_bind(identity, request.signed_transaction)
    except InvalidAppleTransactionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Apple transaction",
        ) from exc
    except AppleTransactionAlreadyLinkedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Apple subscription is already linked",
        ) from exc


@fastapi_app.get(
    "/api/v1/billing/entitlement",
    tags=["billing"],
    response_model=ProEntitlementView,
    summary="Return the current account's server-verified Anke Pro entitlement",
)
async def pro_entitlement(
    identity: AuthenticatedIdentity = Depends(current_identity),
    service: AppleBillingService = Depends(billing_service),
) -> ProEntitlementView:
    return service.entitlement(identity)


@fastapi_app.post(
    "/api/v1/billing/apple/notifications",
    tags=["billing"],
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Receive App Store Server Notifications V2",
)
async def apple_server_notification(
    request: AppleServerNotificationRequest,
    service: AppleBillingService = Depends(billing_service),
) -> Response:
    try:
        service.process_notification(request.signed_payload)
    except InvalidAppleTransactionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Apple notification",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _require_admin_idempotency_key(value: str | None) -> str:
    if not value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )
    try:
        UUID(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key must be a UUID",
        ) from exc
    return value


@fastapi_app.get(
    "/internal/admin/v1/overview",
    tags=["admin"],
    response_model=AdminOverviewResponse,
    summary="Return non-financial administrative counters",
)
async def admin_overview(
    identity: AuthenticatedIdentity = Depends(current_admin),
    service: AdminService = Depends(admin_service),
) -> AdminOverviewResponse:
    return service.overview()


@fastapi_app.get(
    "/internal/admin/v1/users",
    tags=["admin"],
    response_model=AdminUserListResponse,
    summary="Search identity metadata without household financial data",
)
async def admin_users(
    q: str = Query(min_length=1, max_length=120),
    status_filter: AdminUserStatus = Query(default=AdminUserStatus.all, alias="status"),
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=2048),
    identity: AuthenticatedIdentity = Depends(current_admin),
    service: AdminService = Depends(admin_service),
) -> AdminUserListResponse:
    try:
        return service.list_users(q, status_filter, limit, cursor)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid cursor") from exc


@fastapi_app.get(
    "/internal/admin/v1/users/{uid}",
    tags=["admin"],
    response_model=AdminUserDetail,
    summary="Read one identity profile and effective entitlement",
)
async def admin_user(
    uid: str,
    identity: AuthenticatedIdentity = Depends(current_admin),
    service: AdminService = Depends(admin_service),
) -> AdminUserDetail:
    try:
        return service.user_detail(uid)
    except AdminTargetNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found") from exc


@fastapi_app.get(
    "/internal/admin/v1/users/{uid}/entitlement",
    tags=["admin"],
    response_model=AdminUserEntitlementResponse,
    summary="Read the provider and manual Pro source breakdown",
)
async def admin_user_entitlement(
    uid: str,
    identity: AuthenticatedIdentity = Depends(current_admin),
    service: AdminService = Depends(admin_service),
) -> AdminUserEntitlementResponse:
    try:
        return service.entitlement_detail(uid)
    except AdminTargetNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found") from exc


@fastapi_app.post(
    "/internal/admin/v1/users/{uid}/manual-pro-grants",
    tags=["admin"],
    response_model=AdminGrantMutationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an auditable manual Pro grant",
)
async def admin_create_manual_grant(
    uid: str,
    http_request: Request,
    request: AdminGrantCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    identity: AuthenticatedIdentity = Depends(current_admin),
    service: AdminService = Depends(admin_service),
) -> AdminGrantMutationResponse:
    key = _require_admin_idempotency_key(idempotency_key)
    try:
        return service.create_manual_grant(
            identity,
            uid,
            request,
            key,
            http_request.headers.get("X-Request-ID"),
        )
    except AdminTargetNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found") from exc
    except AdminTargetNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Account is not ready for Pro access") from exc
    except AdminIdempotencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Idempotency key conflict") from exc
    except AdminInvalidGrantPeriodError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid grant period") from exc


@fastapi_app.post(
    "/internal/admin/v1/users/{uid}/manual-pro-grants/{grant_id}/revoke",
    tags=["admin"],
    response_model=AdminGrantMutationResponse,
    summary="Revoke one manual Pro grant without touching Apple evidence",
)
async def admin_revoke_manual_grant(
    uid: str,
    grant_id: str,
    http_request: Request,
    request: AdminGrantRevokeRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    identity: AuthenticatedIdentity = Depends(current_admin),
    service: AdminService = Depends(admin_service),
) -> AdminGrantMutationResponse:
    key = _require_admin_idempotency_key(idempotency_key)
    try:
        return service.revoke_manual_grant(
            identity,
            uid,
            grant_id,
            request,
            key,
            http_request.headers.get("X-Request-ID"),
        )
    except AdminTargetNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found") from exc
    except AdminGrantNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Manual grant not found") from exc
    except AdminGrantAlreadyRevokedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Manual grant is already revoked") from exc


@fastapi_app.get(
    "/internal/admin/v1/audit",
    tags=["admin"],
    response_model=AdminAuditListResponse,
    summary="List redacted administrative actions",
)
async def admin_audit(
    uid: str | None = Query(default=None, max_length=256),
    action: str | None = Query(default=None, max_length=120),
    outcome: str | None = Query(default=None, max_length=32),
    from_date: datetime | None = Query(default=None, alias="from"),
    to_date: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=2048),
    identity: AuthenticatedIdentity = Depends(current_admin),
    service: AdminService = Depends(admin_service),
) -> AdminAuditListResponse:
    try:
        return service.audit(
            uid=uid,
            action=action,
            outcome=outcome,
            from_at=from_date,
            to_at=to_date,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid cursor") from exc


@fastapi_app.get(
    "/internal/admin/v1/entitlements",
    tags=["admin"],
    response_model=AdminManualGrantListResponse,
    summary="List manual Pro grants for operational follow-up",
)
async def admin_entitlements(
    grant_status: str | None = Query(default=None, alias="status", pattern="^(active|expired|revoked)$"),
    expiring_within_days: int | None = Query(default=None, ge=1, le=366),
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=2048),
    identity: AuthenticatedIdentity = Depends(current_admin),
    service: AdminService = Depends(admin_service),
) -> AdminManualGrantListResponse:
    try:
        return service.list_manual_grants(
            status=grant_status,
            expiring_within_days=expiring_within_days,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid cursor") from exc


@fastapi_app.patch(
    "/api/v1/me",
    tags=["identity"],
    response_model=AnkeSessionResponse,
    summary="Update the signed-in owner's display name",
)
async def update_me(
    request: ProfileUpdateRequest,
    identity: AuthenticatedIdentity = Depends(current_identity),
    service: AuthService = Depends(auth_service),
) -> AnkeSessionResponse:
    try:
        return service.update_profile(identity, request.display_name)
    except ConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service unavailable",
        ) from exc


@fastapi_app.delete(
    "/api/v1/account",
    tags=["identity"],
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Permanently erase the owner account and Anke Cloud data",
)
async def delete_account(
    identity: AuthenticatedIdentity = Depends(current_identity),
    service: CloudService = Depends(cloud_service),
    clerk: ClerkManagementClient = Depends(clerk_management_service),
) -> Response:
    try:
        clerk.delete_user(identity.provider_subject)
    except ClerkManagementError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Account management service unavailable",
        ) from exc
    except ConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service unavailable",
        ) from exc
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


@fastapi_app.put(
    "/api/v1/devices/push-token",
    tags=["cloud"],
    summary="Register or refresh this device's APNs token",
)
async def register_push_token(
    registration: PushTokenRegistration,
    identity: AuthenticatedIdentity = Depends(current_identity),
    service: CloudService = Depends(cloud_service),
) -> dict:
    try:
        service.register_push_token(identity, registration)
    except MembershipRequiredError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Bootstrap required") from exc
    except DeviceRegistrationRequiredError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Device bootstrap required") from exc
    return {}


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


@fastapi_app.post(
    "/agent/v1/ledger/entries/batch",
    tags=["agent"],
    response_model=AgentLedgerBatchCreateResponse,
    summary="Append up to 25 independently idempotent ledger entries",
)
async def agent_create_ledger_batch(
    request: AgentLedgerBatchCreate,
    principal: AgentPrincipal = Depends(current_agent),
    service: CloudService = Depends(cloud_service),
) -> AgentLedgerBatchCreateResponse:
    try:
        return service.agent_create_ledger_batch(principal, request)
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
    cursor: str | None = Query(default=None, max_length=16384),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    principal: AgentPrincipal = Depends(current_agent),
    service: CloudService = Depends(cloud_service),
) -> AgentEntityListResponse:
    try:
        return service.agent_list_ledger_entries(
            principal,
            limit,
            cursor,
            start_date,
            end_date,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient agent scope") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@fastapi_app.get(
    "/agent/v1/assets",
    tags=["agent"],
    response_model=AgentEntityListResponse,
    summary="Read asset accounts and snapshots with agent scope",
)
async def agent_list_assets(
    limit: int = Query(default=200, ge=1, le=500),
    cursor: str | None = Query(default=None, max_length=16384),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    principal: AgentPrincipal = Depends(current_agent),
    service: CloudService = Depends(cloud_service),
) -> AgentEntityListResponse:
    try:
        return service.agent_list_assets(
            principal,
            limit,
            cursor,
            start_date,
            end_date,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient agent scope") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@fastapi_app.post(
    "/agent/v1/assets",
    tags=["agent"],
    response_model=AgentAssetCreateResponse,
    summary="Create one asset account with an initial snapshot",
)
async def agent_create_asset(
    request: AgentAssetCreate,
    principal: AgentPrincipal = Depends(current_agent),
    service: CloudService = Depends(cloud_service),
) -> AgentAssetCreateResponse:
    try:
        return service.agent_create_asset(principal, request)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient agent scope") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@fastapi_app.post(
    "/agent/v1/assets/batch",
    tags=["agent"],
    response_model=AgentAssetBatchCreateResponse,
    summary="Create up to 25 independently idempotent asset accounts",
)
async def agent_create_asset_batch(
    request: AgentAssetBatchCreate,
    principal: AgentPrincipal = Depends(current_agent),
    service: CloudService = Depends(cloud_service),
) -> AgentAssetBatchCreateResponse:
    try:
        return service.agent_create_asset_batch(principal, request)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient agent scope") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


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
