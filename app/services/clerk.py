from __future__ import annotations

import logging
from typing import Protocol
from urllib.parse import quote

import httpx

from app.config import Settings


logger = logging.getLogger(__name__)


class ClerkManagementError(RuntimeError):
    """Raised when Clerk account management cannot be completed."""


class ClerkHTTPClient(Protocol):
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

    def delete_user(self, provider_subject: str) -> None:
        self._settings.require_clerk_management()
        endpoint = (
            f"{self._settings.clerk_backend_api_url.rstrip('/')}/v1/users/"
            f"{quote(provider_subject, safe='')}"
        )
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._settings.clerk_secret_key}",
        }
        try:
            if self._client is not None:
                response = self._client.delete(endpoint, headers=headers, timeout=10)
            else:
                with httpx.Client(timeout=10) as client:
                    response = client.delete(endpoint, headers=headers)
            if response.status_code == 404:
                return
            if 200 <= response.status_code < 300:
                return
            logger.warning(
                "Clerk user deletion rejected: status_code=%s",
                response.status_code,
            )
            raise ClerkManagementError("Clerk account deletion failed")
        except httpx.HTTPError as exc:
            logger.warning(
                "Clerk user deletion transport failed: error_type=%s",
                type(exc).__name__,
            )
            raise ClerkManagementError("Clerk account deletion failed") from exc
