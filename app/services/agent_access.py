from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import secrets
from uuid import UUID, uuid4

from app.models import (
    Actor,
    ActorType,
    AgentAccessToken,
    AgentConnectionCreate,
    AgentConnectionCreated,
    AgentConnectionView,
    AgentPrincipal,
)
from app.storage.protocols import HouseholdStorage


class InvalidAgentTokenError(RuntimeError):
    pass


class AgentRateLimitExceededError(RuntimeError):
    pass


class AgentAccessService:
    def __init__(
        self,
        storage: HouseholdStorage,
        *,
        requests_per_minute: int = 120,
        failed_auth_threshold: int = 5,
    ):
        self._storage = storage
        self._requests_per_minute = requests_per_minute
        self._failed_auth_threshold = failed_auth_threshold

    def create_connection(
        self,
        household_id: str,
        owner_uid: str,
        request: AgentConnectionCreate,
    ) -> AgentConnectionCreated:
        now = datetime.now(UTC)
        connection_id = uuid4()
        token = self._new_access_token(household_id, connection_id)
        refresh_token = self._new_refresh_token(household_id, connection_id)
        token_expires_at = min(
            now + timedelta(minutes=15),
            now + timedelta(seconds=request.grant_duration_seconds or 0),
        )
        view = self._storage.create_agent_connection(
            household_id,
            Actor(type=ActorType.user, id=owner_uid),
            request,
            str(connection_id),
            self.hash_token(token),
            self.hash_token(refresh_token),
            token_expires_at,
            now,
        )
        return AgentConnectionCreated(
            **view.model_dump(),
            access_token=token,
            token_expires_at=token_expires_at,
            refresh_token=refresh_token,
        )

    def refresh(self, refresh_token: str) -> AgentAccessToken:
        household_id, connection_id = self._parse_refresh_token(refresh_token)
        now = datetime.now(UTC)
        token = self._new_access_token(household_id, connection_id)
        refreshed = self._storage.refresh_agent_token(
            str(household_id),
            str(connection_id),
            self.hash_token(refresh_token),
            self.hash_token(token),
            now + timedelta(minutes=15),
            now,
        )
        if refreshed is None:
            self._storage.record_agent_auth_failure(
                str(household_id),
                str(connection_id),
                "revokedExpiredOrInvalidRefresh",
                now,
                self._failed_auth_threshold,
                5 * 60,
            )
            raise InvalidAgentTokenError("Invalid or expired agent refresh token")
        _, token_expires_at = refreshed
        return AgentAccessToken(access_token=token, token_expires_at=token_expires_at)

    def authenticate(self, token: str) -> AgentPrincipal:
        parts = token.split(".", maxsplit=2)
        if len(parts) != 3:
            raise InvalidAgentTokenError("Malformed agent token")
        try:
            household_id = str(UUID(parts[0]))
            connection_id = str(UUID(parts[1]))
        except ValueError as exc:
            raise InvalidAgentTokenError("Malformed agent token") from exc
        now = datetime.now(UTC)
        principal = self._storage.authenticate_agent_token(
            household_id,
            connection_id,
            self.hash_token(token),
            now,
        )
        if principal is None:
            self._storage.record_agent_auth_failure(
                household_id,
                connection_id,
                "revokedExpiredOrInvalid",
                now,
                self._failed_auth_threshold,
                5 * 60,
            )
            raise InvalidAgentTokenError("Invalid or expired agent token")
        if not self._storage.consume_agent_request(
            household_id,
            connection_id,
            now,
            self._requests_per_minute,
            60,
        ):
            raise AgentRateLimitExceededError("Agent request rate limit exceeded")
        return principal

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _new_access_token(household_id: str | UUID, connection_id: str | UUID) -> str:
        return f"{household_id}.{connection_id}.{secrets.token_urlsafe(32)}"

    @staticmethod
    def _new_refresh_token(household_id: str | UUID, connection_id: str | UUID) -> str:
        return f"r.{household_id}.{connection_id}.{secrets.token_urlsafe(48)}"

    @staticmethod
    def _parse_refresh_token(token: str) -> tuple[UUID, UUID]:
        parts = token.split(".", maxsplit=3)
        if len(parts) != 4 or parts[0] != "r":
            raise InvalidAgentTokenError("Malformed agent refresh token")
        try:
            return UUID(parts[1]), UUID(parts[2])
        except ValueError as exc:
            raise InvalidAgentTokenError("Malformed agent refresh token") from exc
