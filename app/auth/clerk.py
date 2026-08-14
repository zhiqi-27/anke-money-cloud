from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Callable
from urllib.request import Request, urlopen

import jwt
from jwt.algorithms import RSAAlgorithm

from app.auth.anke import AuthenticatedIdentity, InvalidTokenError, extract_bearer_token
from app.config import Settings


class InvalidClerkCredentialError(InvalidTokenError):
    """Raised when a Clerk session token cannot establish an Anke identity."""


class ClerkTokenVerifier:
    def __init__(
        self,
        settings: Settings,
        *,
        jwks_loader: Callable[[], dict[str, Any]] | None = None,
    ):
        self._settings = settings
        self._jwks_loader = jwks_loader or self._load_jwks
        self._keys_by_id: dict[str, Any] = {}

    def verify_bearer_token(self, authorization: str) -> AuthenticatedIdentity:
        try:
            return self.verify(extract_bearer_token(authorization))
        except InvalidClerkCredentialError:
            raise
        except Exception as exc:
            raise InvalidClerkCredentialError("Clerk session token is invalid") from exc

    def verify(self, token: str) -> AuthenticatedIdentity:
        self._settings.require_clerk_auth()
        try:
            header = jwt.get_unverified_header(token)
            key_id = header.get("kid")
            if not isinstance(key_id, str) or not key_id:
                raise InvalidClerkCredentialError("Clerk token has no key id")

            key = self._keys_by_id.get(key_id)
            if key is None:
                self._refresh_keys()
                key = self._keys_by_id.get(key_id)
            if key is None:
                raise InvalidClerkCredentialError("Clerk signing key is unavailable")

            decode_options = {
                "require": ["exp", "iat", "iss", "sub"],
                "verify_aud": bool(self._settings.clerk_audience),
            }
            kwargs: dict[str, Any] = {
                "issuer": self._settings.clerk_issuer,
                "options": decode_options,
                "leeway": 5,
            }
            if self._settings.clerk_audience:
                kwargs["audience"] = self._settings.clerk_audience
            claims = jwt.decode(token, key, algorithms=["RS256"], **kwargs)
        except InvalidClerkCredentialError:
            raise
        except Exception as exc:
            raise InvalidClerkCredentialError("Clerk session token is invalid") from exc

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise InvalidClerkCredentialError("Clerk session token has no subject")

        return AuthenticatedIdentity(
            uid=f"clerk:{subject.strip()}",
            provider="clerk",
            provider_subject=subject.strip(),
            display_name=_display_name(claims),
            email=_email(claims),
        )

    def _refresh_keys(self) -> None:
        payload = self._jwks_loader()
        keys: dict[str, Any] = {}
        for item in payload.get("keys", []):
            if not isinstance(item, dict):
                continue
            key_id = item.get("kid")
            if not isinstance(key_id, str) or item.get("kty") != "RSA":
                continue
            keys[key_id] = RSAAlgorithm.from_jwk(json.dumps(item))
        if not keys:
            raise InvalidClerkCredentialError("Clerk JWKS has no usable keys")
        self._keys_by_id = keys

    def _load_jwks(self) -> dict[str, Any]:
        request = Request(
            self._settings.clerk_jwks_url,
            headers={"Accept": "application/json"},
        )
        with urlopen(request, timeout=10) as response:
            return json.load(response)


def _email(claims: dict[str, Any]) -> str | None:
    for key in ("email", "email_address"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _display_name(claims: dict[str, Any]) -> str | None:
    for key in ("name", "full_name"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    first = claims.get("first_name")
    last = claims.get("last_name")
    parts = [value.strip() for value in (first, last) if isinstance(value, str) and value.strip()]
    return " ".join(parts) or None
