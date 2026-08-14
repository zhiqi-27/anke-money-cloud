from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import jwt

from app.config import Settings


class InvalidTokenError(RuntimeError):
    """Raised when a bearer token cannot establish an Anke identity."""


@dataclass(frozen=True, slots=True)
class AuthenticatedIdentity:
    uid: str
    provider: str = "clerk"
    provider_subject: str = ""
    display_name: str | None = None
    email: str | None = None


class TokenVerifier(Protocol):
    def verify_bearer_token(self, authorization: str) -> AuthenticatedIdentity: ...


def extract_bearer_token(authorization: str) -> str:
    if not authorization:
        raise InvalidTokenError("Authorization header is required")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise InvalidTokenError("Authorization must use Bearer token")
    return token.strip()


class AnkeSessionTokenIssuer:
    ISSUER = "anke-money-cloud"
    AUDIENCE = "anke-money-ios"

    def __init__(self, settings: Settings):
        self._settings = settings

    def issue(
        self,
        identity: AuthenticatedIdentity,
        *,
        now: datetime | None = None,
    ) -> tuple[str, datetime]:
        self._settings.require_session_auth()
        issued_at = (now or datetime.now(UTC)).astimezone(UTC)
        expires_at = issued_at + timedelta(seconds=self._settings.session_ttl_seconds)
        claims: dict[str, Any] = {
            "iss": self.ISSUER,
            "aud": self.AUDIENCE,
            "iat": issued_at,
            "exp": expires_at,
            "sub": identity.uid,
            "uid": identity.uid,
            "provider": identity.provider,
            "providerSubject": identity.provider_subject,
        }
        if identity.display_name:
            claims["displayName"] = identity.display_name
        if identity.email:
            claims["email"] = identity.email
        token = jwt.encode(
            claims,
            self._settings.session_signing_secret,
            algorithm="HS256",
        )
        return token, expires_at


class AnkeSessionTokenVerifier:
    def __init__(self, settings: Settings):
        self._settings = settings

    def verify_bearer_token(self, authorization: str) -> AuthenticatedIdentity:
        token = extract_bearer_token(authorization)
        self._settings.require_session_auth()
        try:
            claims = jwt.decode(
                token,
                self._settings.session_signing_secret,
                algorithms=["HS256"],
                audience=AnkeSessionTokenIssuer.AUDIENCE,
                issuer=AnkeSessionTokenIssuer.ISSUER,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "uid"]},
                leeway=5,
            )
        except Exception as exc:
            raise InvalidTokenError("Anke session token is invalid") from exc

        uid = claims.get("uid") or claims.get("sub")
        provider = claims.get("provider")
        provider_subject = claims.get("providerSubject")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (uid, provider, provider_subject)
        ):
            raise InvalidTokenError("Anke session token has no user identity")
        return AuthenticatedIdentity(
            uid=uid.strip(),
            provider=provider.strip(),
            provider_subject=provider_subject.strip(),
            display_name=_optional_claim(claims, "displayName"),
            email=_optional_claim(claims, "email"),
        )


def _optional_claim(claims: dict[str, Any], key: str) -> str | None:
    value = claims.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None
