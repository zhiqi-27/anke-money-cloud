from __future__ import annotations

from datetime import UTC

from app.auth import AnkeSessionTokenIssuer, AuthenticatedIdentity, ClerkTokenVerifier
from app.models import AnkeSessionResponse
from app.storage.protocols import HouseholdStorage


class AuthService:
    def __init__(
        self,
        storage: HouseholdStorage,
        clerk_verifier: ClerkTokenVerifier,
        session_issuer: AnkeSessionTokenIssuer,
    ):
        self._storage = storage
        self._clerk_verifier = clerk_verifier
        self._session_issuer = session_issuer

    def sign_in_with_clerk(self, authorization: str) -> AnkeSessionResponse:
        identity = self._clerk_verifier.verify_bearer_token(authorization)
        self._storage.ensure_identity(identity)
        return self._session_response(identity)

    def update_profile(
        self,
        identity: AuthenticatedIdentity,
        display_name: str,
    ) -> AnkeSessionResponse:
        normalized = display_name.strip()
        self._storage.update_identity_profile(identity, normalized)
        updated = AuthenticatedIdentity(
            uid=identity.uid,
            provider=identity.provider,
            provider_subject=identity.provider_subject,
            display_name=normalized,
            email=identity.email,
        )
        return self._session_response(updated)

    def _session_response(self, identity: AuthenticatedIdentity) -> AnkeSessionResponse:
        token, expires_at = self._session_issuer.issue(identity)
        return AnkeSessionResponse(
            access_token=token,
            expires_at=expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            uid=identity.uid,
            provider=identity.provider,
            display_name=identity.display_name,
            email=identity.email,
        )
