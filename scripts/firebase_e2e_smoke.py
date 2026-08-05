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


def validate_remote_smoke_target(base_url: str, expected_host: str) -> str:
    if not base_url:
        return ""
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ConfigurationError("Remote Firebase smoke requires an HTTPS base URL")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ConfigurationError("Remote Firebase smoke base URL must not include a path")
    if not expected_host or parsed.hostname.lower() != expected_host.lower():
        raise ConfigurationError("Remote Firebase smoke host does not match the expected host")
    return base_url.rstrip("/")


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


def call_authenticated_me(id_token: str, uid: str, base_url: str) -> int:
    if not base_url:
        from fastapi.testclient import TestClient
        from app.main import fastapi_app

        response = TestClient(
            fastapi_app,
            raise_server_exceptions=False,
        ).get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {id_token}"},
        )
        status_code = response.status_code
        response_body = response.json()
    else:
        request = urllib.request.Request(
            f"{base_url}/api/v1/me",
            headers={"Authorization": f"Bearer {id_token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status_code = response.status
                response_body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"Authenticated remote /api/v1/me failed with status {exc.code}"
            ) from None
        except urllib.error.URLError:
            raise RuntimeError("Authenticated remote /api/v1/me network failure") from None

    if status_code != 200 or response_body != {"uid": uid}:
        raise RuntimeError(
            f"Authenticated /api/v1/me failed with status {status_code}"
        )
    return status_code


def main() -> int:
    settings = Settings.from_environment()
    web_api_key = os.getenv("ANKE_FIREBASE_WEB_API_KEY", "").strip()
    allow_synthetic_user = (
        os.getenv("ANKE_FIREBASE_ALLOW_SYNTHETIC_USER", "").strip().lower()
        == "true"
    )
    base_url = validate_remote_smoke_target(
        os.getenv("ANKE_FIREBASE_SMOKE_BASE_URL", "").strip(),
        os.getenv("ANKE_FIREBASE_SMOKE_EXPECTED_HOST", "").strip(),
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

        call_authenticated_me(id_token, uid, base_url)
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
    print(f"target={'remote' if base_url else 'local'}")
    if base_url:
        print(f"target_host={urllib.parse.urlsplit(base_url).hostname}")
    print("status=200")
    print(f"synthetic_user_deleted={str(cleanup_passed).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
