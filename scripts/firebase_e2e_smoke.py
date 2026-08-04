from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from firebase_admin import auth


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.auth import FirebaseTokenVerifier  # noqa: E402
from app.config import ConfigurationError, Settings  # noqa: E402


def validate_synthetic_auth_target(
    settings: Settings,
    *,
    web_api_key: str,
    allow_synthetic_user: bool,
) -> None:
    if settings.environment != "dev":
        raise ConfigurationError("Synthetic Firebase auth smoke requires dev")
    settings.require_firebase()
    if not web_api_key:
        raise ConfigurationError("ANKE_FIREBASE_WEB_API_KEY is required")
    if not allow_synthetic_user:
        raise ConfigurationError(
            "ANKE_FIREBASE_ALLOW_SYNTHETIC_USER=true is required"
        )


def exchange_custom_token(custom_token: bytes, web_api_key: str) -> str:
    url = (
        "https://identitytoolkit.googleapis.com/v1/"
        "accounts:signInWithCustomToken?key="
        + urllib.parse.quote(web_api_key, safe="")
    )
    payload = json.dumps(
        {
            "token": custom_token.decode("utf-8"),
            "returnSecureToken": True,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_code = f"HTTP_{exc.code}"
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
            candidate = error_payload.get("error", {}).get("message", "")
            if isinstance(candidate, str) and re.fullmatch(
                r"[A-Z0-9_ :.-]{1,160}",
                candidate,
            ):
                error_code = candidate
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        raise RuntimeError(
            f"Firebase custom-token exchange failed: {error_code}"
        ) from None
    except urllib.error.URLError:
        raise RuntimeError("Firebase custom-token exchange network failure") from None
    id_token = result.get("idToken")
    if not isinstance(id_token, str) or not id_token:
        raise RuntimeError("Firebase exchange returned no ID token")
    return id_token


def main() -> int:
    settings = Settings.from_environment()
    web_api_key = os.getenv("ANKE_FIREBASE_WEB_API_KEY", "").strip()
    allow_synthetic_user = (
        os.getenv("ANKE_FIREBASE_ALLOW_SYNTHETIC_USER", "").strip().lower()
        == "true"
    )
    validate_synthetic_auth_target(
        settings,
        web_api_key=web_api_key,
        allow_synthetic_user=allow_synthetic_user,
    )

    verifier = FirebaseTokenVerifier(settings)
    firebase_app = verifier._firebase_app()
    uid = f"smoke-backend-{uuid4()}"
    user_created = False
    cleanup_passed = False
    try:
        custom_token = auth.create_custom_token(uid, app=firebase_app)
        id_token = exchange_custom_token(custom_token, web_api_key)
        user_created = True

        from app.main import fastapi_app

        response = TestClient(
            fastapi_app,
            raise_server_exceptions=False,
        ).get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {id_token}"},
        )
        if response.status_code != 200 or response.json() != {"uid": uid}:
            raise RuntimeError(
                f"Authenticated /api/v1/me failed with status {response.status_code}"
            )
    finally:
        if user_created:
            if not uid.startswith("smoke-backend-"):
                raise RuntimeError("Refusing to clean up a non-smoke Firebase user")
            auth.delete_user(uid, app=firebase_app)
            cleanup_passed = True

    print("Development Firebase auth smoke passed")
    print(f"project_id={settings.firebase_project_id}")
    print(f"uid={uid}")
    print("endpoint=/api/v1/me")
    print("status=200")
    print(f"synthetic_user_deleted={str(cleanup_passed).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
