from __future__ import annotations

from functools import lru_cache

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth import (
    AuthenticatedIdentity,
    FirebaseTokenVerifier,
    InvalidTokenError,
    TokenVerifier,
)
from app.config import ConfigurationError, get_settings
from app.services import (
    AgentAccessService,
    AgentRateLimitExceededError,
    CloudService,
    InvalidAgentTokenError,
)
from app.models import AgentPrincipal
from app.storage.cosmos import CosmosHouseholdStorage


firebase_bearer = HTTPBearer(
    auto_error=False,
    bearerFormat="Firebase ID token",
    description="Firebase ID token for the Anke Money Firebase project.",
)

agent_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="AgentBearer",
    bearerFormat="Anke Skill API key",
    description="The full-capability, owner-revocable Anke Money Skill API key.",
)


@lru_cache(maxsize=1)
def get_token_verifier() -> TokenVerifier:
    return FirebaseTokenVerifier(get_settings())


@lru_cache(maxsize=1)
def get_household_storage() -> CosmosHouseholdStorage:
    return CosmosHouseholdStorage(get_settings())


def cloud_service() -> CloudService:
    return CloudService(get_household_storage())


def agent_access_service() -> AgentAccessService:
    settings = get_settings()
    return AgentAccessService(
        get_household_storage(),
        requests_per_minute=settings.agent_requests_per_minute,
        failed_auth_threshold=settings.agent_failed_auth_threshold,
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
    credentials: HTTPAuthorizationCredentials | None = Security(firebase_bearer),
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
