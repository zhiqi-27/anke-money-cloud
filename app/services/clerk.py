from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from app.config import Settings


logger = logging.getLogger(__name__)


class ClerkManagementError(RuntimeError):
    """Raised when Clerk account management cannot be completed."""


@dataclass(frozen=True, slots=True)
class ClerkDirectoryUser:
    """PII-minimized identity projection returned by the Clerk Backend API."""

    provider_subject: str
    email: str | None = None
    display_name: str | None = None
    created_at: datetime | None = None


class ClerkHTTPClient(Protocol):
    def get(self, url: str, **kwargs) -> httpx.Response: ...

    def delete(self, url: str, **kwargs) -> httpx.Response: ...


class ClerkManagementClient:
    def __init__(
        self,
        settings: Settings,
        *,
        client: ClerkHTTPClient | None = None,
    ):
        self._settings = settings
        self._client = client

    def search_users(self, query: str, limit: int = 25) -> list[ClerkDirectoryUser]:
        """Search the Clerk directory without exposing it to the browser."""
        self._settings.require_clerk_management()
        normalized = query.strip()
        if not normalized:
            return []
        endpoint = f"{self._settings.clerk_backend_api_url.rstrip('/')}/v1/users"
        response = self._request(
            "get",
            endpoint,
            params={"query": normalized, "limit": min(max(limit, 1), 100)},
        )
        if response.status_code < 200 or response.status_code >= 300:
            self._raise_rejected("Clerk user search", response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ClerkManagementError("Clerk user search returned invalid data") from exc
        if isinstance(payload, dict):
            payload = payload.get("data", [])
        if not isinstance(payload, list):
            raise ClerkManagementError("Clerk user search returned invalid data")
        return [user for item in payload if (user := self._directory_user(item)) is not None]

    def get_user(self, provider_subject: str) -> ClerkDirectoryUser | None:
        """Read one Clerk directory identity by its provider subject."""
        self._settings.require_clerk_management()
        endpoint = (
            f"{self._settings.clerk_backend_api_url.rstrip('/')}/v1/users/"
            f"{quote(provider_subject, safe='')}"
        )
        response = self._request("get", endpoint)
        if response.status_code == 404:
            return None
        if response.status_code < 200 or response.status_code >= 300:
            self._raise_rejected("Clerk user lookup", response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ClerkManagementError("Clerk user lookup returned invalid data") from exc
        return self._directory_user(payload)

    def delete_user(self, provider_subject: str) -> None:
        self._settings.require_clerk_management()
        endpoint = (
            f"{self._settings.clerk_backend_api_url.rstrip('/')}/v1/users/"
            f"{quote(provider_subject, safe='')}"
        )
        response = self._request("delete", endpoint)
        if response.status_code == 404:
            return
        if 200 <= response.status_code < 300:
            return
        self._raise_rejected("Clerk account deletion", response)

    def _request(self, method: str, endpoint: str, **kwargs) -> httpx.Response:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._settings.clerk_secret_key}",
        }
        kwargs.setdefault("headers", headers)
        kwargs.setdefault("timeout", 10)
        try:
            if self._client is not None:
                return getattr(self._client, method)(endpoint, **kwargs)
            with httpx.Client(timeout=10) as client:
                return getattr(client, method)(endpoint, **kwargs)
        except httpx.HTTPError as exc:
            logger.warning(
                "Clerk management transport failed: operation=%s error_type=%s",
                method,
                type(exc).__name__,
            )
            raise ClerkManagementError("Clerk management request failed") from exc

    @staticmethod
    def _raise_rejected(operation: str, response: httpx.Response) -> None:
        logger.warning(
            "Clerk management request rejected: operation=%s status_code=%s",
            operation,
            response.status_code,
        )
        raise ClerkManagementError(f"{operation} failed")

    @classmethod
    def _directory_user(cls, payload: Any) -> ClerkDirectoryUser | None:
        if not isinstance(payload, dict):
            return None
        subject = payload.get("id")
        if not isinstance(subject, str) or not subject.strip():
            return None

        email: str | None = None
        addresses = payload.get("email_addresses")
        if isinstance(addresses, list):
            primary_id = payload.get("primary_email_address_id")
            candidates = [
                item for item in addresses
                if isinstance(item, dict) and item.get("id") == primary_id
            ]
            if not candidates:
                candidates = [item for item in addresses if isinstance(item, dict)]
            for item in candidates:
                value = item.get("email_address")
                if isinstance(value, str) and value.strip():
                    email = value.strip()
                    break
        if email is None:
            value = payload.get("email")
            if isinstance(value, str) and value.strip():
                email = value.strip()

        first = payload.get("first_name")
        last = payload.get("last_name")
        parts = [
            value.strip()
            for value in (first, last)
            if isinstance(value, str) and value.strip()
        ]
        display_name = " ".join(parts) or None
        if display_name is None:
            for key in ("username", "name"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    display_name = value.strip()
                    break

        return ClerkDirectoryUser(
            provider_subject=subject.strip(),
            email=email,
            display_name=display_name,
            created_at=cls._created_at(payload.get("created_at")),
        )

    @staticmethod
    def _created_at(value: Any) -> datetime | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            # Clerk timestamps are Unix epoch milliseconds. Guard against an
            # accidental seconds value so malformed data cannot crash the list.
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            try:
                return datetime.fromtimestamp(timestamp, tz=UTC)
            except (OverflowError, OSError, ValueError):
                return None
        if isinstance(value, str) and value.strip().isdigit():
            return ClerkManagementClient._created_at(int(value.strip()))
        return None
