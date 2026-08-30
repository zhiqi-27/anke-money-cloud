from __future__ import annotations

import base64
import binascii
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
from app.services.billing import ProEntitlementRequiredError


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
        entitlement_checker=None,
    ):
        self._storage = storage
        self._requests_per_minute = requests_per_minute
        self._failed_auth_threshold = failed_auth_threshold
        self._entitlement_checker = entitlement_checker

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
        api_key = self._new_api_key(household_id)
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
        household_id, connection_id = self._token_coordinates(token)
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
        if self._entitlement_checker is not None and not self._entitlement_checker(
            str(principal.household_id)
        ):
            raise ProEntitlementRequiredError("An active Anke Pro subscription is required")
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
    def _new_api_key(household_id: str | UUID) -> str:
        compact_household_id = base64.urlsafe_b64encode(
            UUID(str(household_id)).bytes
        ).decode("ascii").rstrip("=")
        return f"ank_{compact_household_id}.{secrets.token_urlsafe(24)}"

    @staticmethod
    def _token_coordinates(token: str) -> tuple[str, str]:
        if not token.startswith("ank_"):
            raise InvalidAgentTokenError("Malformed agent API key")

        payload = token.removeprefix("ank_")
        compact_household_id, separator, secret = payload.partition(".")
        if separator:
            try:
                decoded = base64.b64decode(
                    compact_household_id + "==",
                    altchars=b"-_",
                    validate=True,
                )
                household_uuid = UUID(bytes=decoded)
            except (binascii.Error, ValueError) as exc:
                raise InvalidAgentTokenError("Malformed agent API key") from exc
            if (
                len(compact_household_id) != 22
                or len(secret) != 32
                or any(
                    not (character.isalnum() or character in "-_")
                    for character in secret
                )
            ):
                raise InvalidAgentTokenError("Malformed agent API key")
            household_id = str(household_uuid)
            connection_id = str(uuid5(
                NAMESPACE_URL,
                f"anke-agent-api-key:{household_id}",
            ))
            return household_id, connection_id

        # Compatibility for full-capability keys created before the compact format.
        parts = token.split("_", maxsplit=3)
        if len(parts) != 4 or parts[0] != "ank" or not parts[3]:
            raise InvalidAgentTokenError("Malformed agent API key")
        try:
            return str(UUID(parts[1])), str(UUID(parts[2]))
        except ValueError as exc:
            raise InvalidAgentTokenError("Malformed agent API key") from exc
