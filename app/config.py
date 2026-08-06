from __future__ import annotations

import os
from dataclasses import dataclass


TRUE_VALUES = {"1", "true", "yes", "on"}
ALLOWED_ENVIRONMENTS = {"local", "dev", "prod", "test"}


class ConfigurationError(RuntimeError):
    """Raised when required runtime configuration is absent or unsafe."""


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def _csv(name: str, default: str) -> tuple[str, ...]:
    value = os.getenv(name, default)
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _integer(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str
    firebase_project_id: str
    firebase_check_revoked: bool
    cosmos_endpoint: str
    cosmos_database: str
    cosmos_entities_container: str
    cosmos_identities_container: str
    cosmos_key: str
    cosmos_expected_account_name: str
    cosmos_allow_smoke_write: bool
    agent_requests_per_minute: int = 120
    agent_failed_auth_threshold: int = 5
    mcp_allowed_hosts: tuple[str, ...] = (
        "testserver",
        "localhost:*",
        "127.0.0.1:*",
    )
    mcp_allowed_origins: tuple[str, ...] = ()

    @classmethod
    def from_environment(cls) -> "Settings":
        environment = os.getenv("ANKE_ENVIRONMENT", "local").strip().lower()
        if environment not in ALLOWED_ENVIRONMENTS:
            raise ConfigurationError(
                f"ANKE_ENVIRONMENT must be one of {sorted(ALLOWED_ENVIRONMENTS)}"
            )
        return cls(
            environment=environment,
            firebase_project_id=os.getenv("ANKE_FIREBASE_PROJECT_ID", "").strip(),
            firebase_check_revoked=_boolean(
                "ANKE_FIREBASE_CHECK_REVOKED",
                default=environment not in {"local", "test"},
            ),
            cosmos_endpoint=os.getenv("ANKE_COSMOS_ENDPOINT", "").strip(),
            cosmos_database=os.getenv("ANKE_COSMOS_DATABASE", "anke_money_dev").strip(),
            cosmos_entities_container=os.getenv(
                "ANKE_COSMOS_ENTITIES_CONTAINER", "anke_entities"
            ).strip(),
            cosmos_identities_container=os.getenv(
                "ANKE_COSMOS_IDENTITIES_CONTAINER", "anke_identities"
            ).strip(),
            cosmos_key=os.getenv("ANKE_COSMOS_KEY", "").strip(),
            cosmos_expected_account_name=os.getenv(
                "ANKE_COSMOS_EXPECTED_ACCOUNT_NAME", ""
            ).strip(),
            cosmos_allow_smoke_write=_boolean(
                "ANKE_COSMOS_ALLOW_SMOKE_WRITE", default=False
            ),
            agent_requests_per_minute=_integer(
                "ANKE_AGENT_REQUESTS_PER_MINUTE", 120, minimum=10, maximum=10_000
            ),
            agent_failed_auth_threshold=_integer(
                "ANKE_AGENT_FAILED_AUTH_THRESHOLD", 5, minimum=3, maximum=100
            ),
            mcp_allowed_hosts=_csv(
                "ANKE_MCP_ALLOWED_HOSTS",
                "testserver,localhost:*,127.0.0.1:*",
            ),
            mcp_allowed_origins=_csv("ANKE_MCP_ALLOWED_ORIGINS", ""),
        )

    @property
    def docs_enabled(self) -> bool:
        return self.environment in {"local", "dev", "test"}

    def require_firebase(self) -> None:
        if not self.firebase_project_id:
            raise ConfigurationError("ANKE_FIREBASE_PROJECT_ID is required for auth")

    def require_cosmos(self) -> None:
        missing = [
            name
            for name, value in (
                ("ANKE_COSMOS_ENDPOINT", self.cosmos_endpoint),
                ("ANKE_COSMOS_DATABASE", self.cosmos_database),
                ("ANKE_COSMOS_ENTITIES_CONTAINER", self.cosmos_entities_container),
            )
            if not value
        ]
        if missing:
            raise ConfigurationError(f"Missing Cosmos settings: {', '.join(missing)}")


def get_settings() -> Settings:
    return Settings.from_environment()


def get_firebase_credentials_json() -> str:
    """Return a protected Firebase credential payload without logging it."""
    return os.getenv("ANKE_FIREBASE_CREDENTIALS_JSON", "").strip()
