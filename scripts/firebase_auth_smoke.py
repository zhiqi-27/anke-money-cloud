from __future__ import annotations

import getpass
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.auth import FirebaseTokenVerifier  # noqa: E402
from app.config import ConfigurationError, Settings  # noqa: E402


def validate_firebase_smoke_target(settings: Settings) -> None:
    if settings.environment not in {"local", "dev"}:
        raise ConfigurationError("Firebase smoke is limited to local or dev")
    settings.require_firebase()


def main() -> int:
    settings = Settings.from_environment()
    validate_firebase_smoke_target(settings)
    token = getpass.getpass("Firebase Development ID token (hidden): ").strip()
    if not token:
        raise ConfigurationError("A Firebase ID token is required")

    identity = FirebaseTokenVerifier(settings).verify_bearer_token(
        f"Bearer {token}"
    )
    print("Development Firebase auth smoke passed")
    print(f"project_id={settings.firebase_project_id}")
    print(f"uid={identity.uid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
