from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import firebase_admin
from firebase_admin import auth, credentials

from app.config import Settings, get_firebase_credentials_json


logger = logging.getLogger(__name__)
FIREBASE_APP_NAME = "anke-money-cloud"


class InvalidTokenError(RuntimeError):
    """Raised when a Firebase bearer token cannot establish identity."""


@dataclass(frozen=True, slots=True)
class AuthenticatedIdentity:
    uid: str


class TokenVerifier(Protocol):
    def verify_bearer_token(self, authorization: str) -> AuthenticatedIdentity: ...


def extract_bearer_token(authorization: str) -> str:
    if not authorization:
        raise InvalidTokenError("Authorization header is required")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise InvalidTokenError("Authorization must use Bearer token")
    return token.strip()


class FirebaseTokenVerifier:
    """Lazily verifies Firebase ID tokens without import-time network or secrets."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._app: firebase_admin.App | None = None

    def verify_bearer_token(self, authorization: str) -> AuthenticatedIdentity:
        token = extract_bearer_token(authorization)
        self._settings.require_firebase()
        try:
            claims: dict[str, Any] = auth.verify_id_token(
                token,
                app=self._firebase_app(),
                check_revoked=self._settings.firebase_check_revoked,
            )
        except Exception as exc:
            logger.warning(
                "Firebase token verification failed, error_type=%s",
                type(exc).__name__,
            )
            raise InvalidTokenError("Firebase token is invalid") from exc

        uid = claims.get("uid") or claims.get("sub")
        if not isinstance(uid, str) or not uid.strip():
            raise InvalidTokenError("Firebase token has no user identity")
        return AuthenticatedIdentity(uid=uid.strip())

    def _firebase_app(self) -> firebase_admin.App:
        if self._app is not None:
            return self._app
        try:
            self._app = firebase_admin.get_app(FIREBASE_APP_NAME)
        except ValueError:
            credentials_json = get_firebase_credentials_json()
            if credentials_json:
                try:
                    credential_payload = json.loads(credentials_json)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        "ANKE_FIREBASE_CREDENTIALS_JSON is not valid JSON"
                    ) from exc
                credential = credentials.Certificate(credential_payload)
            else:
                credential = credentials.ApplicationDefault()
            self._app = firebase_admin.initialize_app(
                credential,
                options={"projectId": self._settings.firebase_project_id},
                name=FIREBASE_APP_NAME,
            )
        return self._app
