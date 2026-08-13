from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import secrets
from uuid import NAMESPACE_URL, UUID, uuid5

from app.models import (
    Actor,
    ActorType,
    AgentAPIKeyCreated,
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

    def create_api_key(
        self,
        household_id: str,
        owner_uid: str,
    ) -> AgentAPIKeyCreated:
        now = datetime.now(UTC)
        connection_id = uuid5(
            NAMESPACE_URL,
            f"anke-agent-api-key:{household_id}",
        )
        api_key = self._new_api_key(household_id, connection_id)
        view = self._storage.replace_agent_api_key(
            household_id,
            Actor(type=ActorType.user, id=owner_uid),
            str(connection_id),
            self.hash_token(api_key),
            api_key[:13],
            now,
        )
        return AgentAPIKeyCreated(
            **view.model_dump(),
            api_key=api_key,
        )

    def authenticate(self, token: str) -> AgentPrincipal:
        parts = token.split("_", maxsplit=3)
        if len(parts) != 4 or parts[0] != "ank":
            raise InvalidAgentTokenError("Malformed agent API key")
        try:
            household_id = str(UUID(parts[1]))
            connection_id = str(UUID(parts[2]))
        except ValueError as exc:
            raise InvalidAgentTokenError("Malformed agent API key") from exc
        now = datetime.now(UTC)
        principal = self._storage.authenticate_agent_api_key(
            household_id,
            connection_id,
            self.hash_token(token),
            now,
        )
        if principal is None:
            self._storage.record_agent_auth_failure(
                household_id,
                connection_id,
                "revokedOrInvalid",
                now,
                self._failed_auth_threshold,
                5 * 60,
            )
            raise InvalidAgentTokenError("Invalid or revoked agent API key")
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
    def _new_api_key(household_id: str | UUID, connection_id: str | UUID) -> str:
        return f"ank_{household_id}_{connection_id}_{secrets.token_urlsafe(32)}"
