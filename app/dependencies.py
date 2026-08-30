from __future__ import annotations

from functools import lru_cache
from collections import defaultdict, deque
import time
from threading import RLock

from fastapi import Depends, HTTPException, Security, status
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth import (
    AnkeSessionTokenIssuer,
    AnkeSessionTokenVerifier,
    AuthenticatedIdentity,
    ClerkTokenVerifier,
    InvalidTokenError,
    TokenVerifier,
)
from app.config import ConfigurationError, get_settings
from app.services import (
    AgentAccessService,
    AgentRateLimitExceededError,
    CloudService,
    AuthService,
    InvalidAgentTokenError,
    ClerkManagementClient,
    AdminService,
)
from app.services.billing import AppleBillingService, AppleSignedDataVerifier
from app.services.push_notifications import APNsPushNotificationService
from app.models import AgentPrincipal
from app.storage.cosmos import CosmosHouseholdStorage
from app.storage.protocols import HouseholdStorage


anke_session_bearer = HTTPBearer(
    auto_error=False,
    bearerFormat="Anke session token",
    description="Anke Cloud session token issued after Clerk authentication.",
)

clerk_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="ClerkBearer",
    bearerFormat="Clerk session token",
    description="Clerk session token exchanged once for an Anke Cloud session.",
)


_admin_rate_lock = RLock()
_admin_rate_events: dict[str, deque[float]] = defaultdict(deque)

agent_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="AgentBearer",
    bearerFormat="Anke Skill API key",
    description="The full-capability, owner-revocable Anke Money Skill API key.",
)


@lru_cache(maxsize=1)
def get_token_verifier() -> TokenVerifier:
    return AnkeSessionTokenVerifier(get_settings())


@lru_cache(maxsize=1)
def get_clerk_verifier() -> ClerkTokenVerifier:
    return ClerkTokenVerifier(get_settings())


@lru_cache(maxsize=1)
def get_session_issuer() -> AnkeSessionTokenIssuer:
    return AnkeSessionTokenIssuer(get_settings())


@lru_cache(maxsize=1)
def get_household_storage() -> CosmosHouseholdStorage:
    return CosmosHouseholdStorage(get_settings())


@lru_cache(maxsize=4)
def _push_notification_service(
    storage: HouseholdStorage,
) -> APNsPushNotificationService:
    return APNsPushNotificationService(storage, get_settings())


def get_push_notification_service() -> APNsPushNotificationService:
    return _push_notification_service(get_household_storage())


def cloud_service() -> CloudService:
    storage = get_household_storage()
    return CloudService(
        storage,
        change_notifier=get_push_notification_service().notify_household,
        entitlement_checker=storage.has_active_pro_entitlement,
    )


@lru_cache(maxsize=1)
def get_apple_verifier() -> AppleSignedDataVerifier:
    return AppleSignedDataVerifier(get_settings())


def billing_service() -> AppleBillingService:
    return AppleBillingService(get_household_storage(), get_apple_verifier())


def admin_service() -> AdminService:
    return AdminService(get_household_storage())


def auth_service() -> AuthService:
    return AuthService(
        get_household_storage(),
        get_clerk_verifier(),
        get_session_issuer(),
    )


def clerk_management_service() -> ClerkManagementClient:
    return ClerkManagementClient(get_settings())


def agent_access_service() -> AgentAccessService:
    settings = get_settings()
    return AgentAccessService(
        get_household_storage(),
        requests_per_minute=settings.agent_requests_per_minute,
        failed_auth_threshold=settings.agent_failed_auth_threshold,
        entitlement_checker=get_household_storage().has_active_pro_entitlement,
    )


def current_agent(
    credentials: HTTPAuthorizationCredentials | None = Security(agent_bearer),
    access: AgentAccessService = Depends(agent_access_service),
) -> AgentPrincipal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing agent authentication",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        principal = access.authenticate(credentials.credentials)
        return principal
    except AgentRateLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Agent request rate limit exceeded",
            headers={"Retry-After": "60"},
        ) from exc
    except InvalidAgentTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing agent authentication",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def current_identity(
    credentials: HTTPAuthorizationCredentials | None = Security(anke_session_bearer),
) -> AuthenticatedIdentity:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing authentication",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        authorization = f"{credentials.scheme} {credentials.credentials}"
        return get_token_verifier().verify_bearer_token(authorization)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing authentication",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except ConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service unavailable",
        ) from exc


def current_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(clerk_bearer),
) -> AuthenticatedIdentity:
    settings = get_settings()
    if not settings.admin_clerk_subjects:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin service unavailable",
        )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin authentication",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        identity = get_clerk_verifier().verify_bearer_token(
            f"{credentials.scheme} {credentials.credentials}"
        )
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin authentication",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except ConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service unavailable",
        ) from exc
    if identity.provider_subject not in settings.admin_clerk_subjects:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access is not permitted",
        )
    key = f"{identity.provider_subject}:{request.client.host if request.client else 'unknown'}"
    now = time.monotonic()
    with _admin_rate_lock:
        events = _admin_rate_events[key]
        cutoff = now - 60
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= settings.admin_requests_per_minute:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Admin request rate limit exceeded",
                headers={"Retry-After": "60"},
            )
        events.append(now)
    return identity
