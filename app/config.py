from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse


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
    clerk_jwks_url: str
    clerk_issuer: str
    clerk_audience: str
    clerk_secret_key: str
    clerk_backend_api_url: str
    session_signing_secret: str
    session_ttl_seconds: int
    cosmos_endpoint: str
    cosmos_database: str
    cosmos_entities_container: str
    cosmos_identities_container: str
    cosmos_key: str
    cosmos_expected_account_name: str
    cosmos_allow_smoke_write: bool
    apns_team_id: str = ""
    apns_key_id: str = ""
    apns_private_key: str = ""
    apns_topic: str = "app.ankemoney.ios"
    apple_app_id: int = 0
    apple_bundle_id: str = "app.ankemoney.ios"
    apple_product_ids: tuple[str, ...] = (
        "app.ankemoney.ios.pro.monthly",
        "app.ankemoney.ios.pro.yearly",
    )
    apple_root_certificates_base64: tuple[str, ...] = ()
    agent_requests_per_minute: int = 120
    agent_failed_auth_threshold: int = 5
    admin_clerk_subjects: tuple[str, ...] = ()
    admin_requests_per_minute: int = 60
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
        settings = cls(
            environment=environment,
            clerk_jwks_url=os.getenv("CLERK_JWKS_URL", "").strip(),
            clerk_issuer=os.getenv("CLERK_ISSUER", "").strip(),
            clerk_audience=os.getenv("CLERK_AUDIENCE", "").strip(),
            clerk_secret_key=os.getenv("CLERK_SECRET_KEY", "").strip(),
            clerk_backend_api_url=os.getenv(
                "CLERK_BACKEND_API_URL", "https://api.clerk.com"
            ).strip(),
            session_signing_secret=os.getenv("ANKE_SESSION_SIGNING_SECRET", "").strip(),
            session_ttl_seconds=_integer(
                "ANKE_SESSION_TTL_SECONDS", 2_592_000, minimum=300, maximum=31_536_000
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
            apns_team_id=os.getenv("ANKE_APNS_TEAM_ID", "").strip(),
            apns_key_id=os.getenv("ANKE_APNS_KEY_ID", "").strip(),
            apns_private_key=os.getenv("ANKE_APNS_PRIVATE_KEY", "").strip(),
            apns_topic=os.getenv(
                "ANKE_APNS_TOPIC", "app.ankemoney.ios"
            ).strip(),
            apple_app_id=_integer(
                "ANKE_APPLE_APP_ID", 0, minimum=0, maximum=9_999_999_999
            ),
            apple_bundle_id=os.getenv(
                "ANKE_APPLE_BUNDLE_ID", "app.ankemoney.ios"
            ).strip(),
            apple_product_ids=_csv(
                "ANKE_APPLE_PRODUCT_IDS",
                "app.ankemoney.ios.pro.monthly,app.ankemoney.ios.pro.yearly",
            ),
            apple_root_certificates_base64=_csv(
                "ANKE_APPLE_ROOT_CERTIFICATES_BASE64", ""
            ),
            agent_requests_per_minute=_integer(
                "ANKE_AGENT_REQUESTS_PER_MINUTE", 120, minimum=10, maximum=10_000
            ),
            agent_failed_auth_threshold=_integer(
                "ANKE_AGENT_FAILED_AUTH_THRESHOLD", 5, minimum=3, maximum=100
            ),
            admin_clerk_subjects=_csv("ANKE_ADMIN_CLERK_SUBJECTS", ""),
            admin_requests_per_minute=_integer(
                "ANKE_ADMIN_REQUESTS_PER_MINUTE", 60, minimum=10, maximum=1_000
            ),
            mcp_allowed_hosts=_csv(
                "ANKE_MCP_ALLOWED_HOSTS",
                "testserver,localhost:*,127.0.0.1:*",
            ),
            mcp_allowed_origins=_csv("ANKE_MCP_ALLOWED_ORIGINS", ""),
        )
        if environment == "prod":
            settings.require_production_boundary()
        return settings

    @property
    def docs_enabled(self) -> bool:
        return self.environment in {"local", "dev", "test"}

    def require_clerk_auth(self) -> None:
        if not self.clerk_jwks_url:
            raise ConfigurationError("CLERK_JWKS_URL is required for auth")
        if not self.clerk_jwks_url.startswith("https://"):
            raise ConfigurationError("CLERK_JWKS_URL must use HTTPS")
        if not self.clerk_issuer:
            raise ConfigurationError("CLERK_ISSUER is required for auth")
        if not self.clerk_issuer.startswith("https://"):
            raise ConfigurationError("CLERK_ISSUER must use HTTPS")

    def require_clerk_management(self) -> None:
        self.require_clerk_auth()
        if not self.clerk_secret_key:
            raise ConfigurationError("CLERK_SECRET_KEY is required for account management")
        if not self.clerk_backend_api_url.startswith("https://"):
            raise ConfigurationError("CLERK_BACKEND_API_URL must use HTTPS")

    def require_session_auth(self) -> None:
        if len(self.session_signing_secret.encode("utf-8")) < 32:
            raise ConfigurationError(
                "ANKE_SESSION_SIGNING_SECRET must contain at least 32 bytes"
            )

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

    @property
    def apns_configured(self) -> bool:
        return bool(
            self.apns_team_id
            and self.apns_key_id
            and self.apns_private_key
            and self.apns_topic
        )

    def require_production_boundary(self) -> None:
        """Reject unsafe defaults before a Production app can serve traffic."""
        self.require_clerk_management()
        if not self.clerk_secret_key.startswith("sk_live_"):
            raise ConfigurationError(
                "CLERK_SECRET_KEY must be a live Production Clerk key"
            )
        self.require_session_auth()
        self.require_cosmos()
        if self.cosmos_key:
            raise ConfigurationError(
                "ANKE_COSMOS_KEY must be empty in Production; use managed identity"
            )
        if self.cosmos_allow_smoke_write:
            raise ConfigurationError(
                "ANKE_COSMOS_ALLOW_SMOKE_WRITE must be false in Production"
            )
        if not self.cosmos_expected_account_name:
            raise ConfigurationError(
                "ANKE_COSMOS_EXPECTED_ACCOUNT_NAME is required in Production"
            )
        endpoint_host = self.cosmos_endpoint.removeprefix("https://").split("/", 1)[0]
        if not endpoint_host.startswith(self.cosmos_expected_account_name + "."):
            raise ConfigurationError(
                "ANKE_COSMOS_ENDPOINT does not match the expected Production account"
            )
        if not self.apns_configured:
            raise ConfigurationError("Production APNs credentials are required")
        if not self.apple_app_id:
            raise ConfigurationError("ANKE_APPLE_APP_ID is required in Production")
        if self.apple_bundle_id != self.apns_topic:
            raise ConfigurationError("Apple bundle ID must match the APNs topic")
        if len(self.apple_product_ids) != 2:
            raise ConfigurationError("Exactly two Anke Pro product IDs are required")
        if not self.apple_root_certificates_base64:
            raise ConfigurationError(
                "ANKE_APPLE_ROOT_CERTIFICATES_BASE64 is required in Production"
            )
        clerk_values = (self.clerk_issuer, self.clerk_jwks_url)
        unsafe_clerk_values = tuple(
            value for value in clerk_values if not self._is_default_clerk_domain(value)
        )
        unsafe_values = unsafe_clerk_values + (
            self.cosmos_endpoint,
            self.cosmos_database,
            self.cosmos_expected_account_name,
        )
        if any("dev" in value.lower() or "test" in value.lower() for value in unsafe_values):
            raise ConfigurationError(
                "Production settings must not contain Development or test identifiers"
            )

    @staticmethod
    def _is_default_clerk_domain(value: str) -> bool:
        host = urlparse(value).hostname or ""
        return host.endswith(".clerk.accounts.dev")


def get_settings() -> Settings:
    return Settings.from_environment()
