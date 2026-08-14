from __future__ import annotations

from typing import Callable
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.config import Settings


class ClerkManagementError(RuntimeError):
    """Raised when Clerk account management cannot be completed."""


class ClerkManagementClient:
    def __init__(
        self,
        settings: Settings,
        *,
        opener: Callable[..., object] = urlopen,
    ):
        self._settings = settings
        self._opener = opener

    def delete_user(self, provider_subject: str) -> None:
        self._settings.require_clerk_management()
        endpoint = (
            f"{self._settings.clerk_backend_api_url.rstrip('/')}/v1/users/"
            f"{quote(provider_subject, safe='')}"
        )
        request = Request(
            endpoint,
            method="DELETE",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._settings.clerk_secret_key}",
            },
        )
        try:
            with self._opener(request, timeout=10) as response:
                status_code = getattr(response, "status", 200)
                if not 200 <= status_code < 300:
                    raise ClerkManagementError("Clerk account deletion failed")
        except HTTPError as exc:
            if exc.code == 404:
                return
            raise ClerkManagementError("Clerk account deletion failed") from exc
        except ClerkManagementError:
            raise
        except Exception as exc:
            raise ClerkManagementError("Clerk account deletion failed") from exc
