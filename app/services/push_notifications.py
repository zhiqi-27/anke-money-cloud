from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import logging
from threading import RLock
import time

import httpx
import jwt

from app.config import Settings
from app.storage.protocols import HouseholdStorage


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PushNotificationResult:
    attempted: int = 0
    sent: int = 0
    disabled: int = 0
    skipped: int = 0


class APNsPushNotificationService:
    """Send data-free synchronization hints to registered iOS devices."""

    _permanent_token_reasons = {
        "BadDeviceToken",
        "DeviceTokenNotForTopic",
        "Unregistered",
    }

    def __init__(
        self,
        storage: HouseholdStorage,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
    ):
        self._storage = storage
        self._settings = settings
        self._client = client or httpx.Client(http2=True, timeout=10.0)
        self._bearer_token: str | None = None
        self._bearer_created_at = 0.0
        self._lock = RLock()

    def notify_household(self, household_id: str) -> PushNotificationResult:
        registrations = self._storage.active_push_tokens(household_id)
        if not registrations:
            return PushNotificationResult()
        if not self._settings.apns_configured:
            logger.info(
                "APNs notification skipped, household_id=%s reason=not_configured devices=%s",
                household_id,
                len(registrations),
            )
            return PushNotificationResult(skipped=len(registrations))

        attempted = sent = disabled = skipped = 0
        for registration in registrations:
            if registration.get("topic") != self._settings.apns_topic:
                skipped += 1
                continue
            attempted += 1
            response = self._send(registration, household_id)
            reason = self._response_reason(response)
            if response.status_code == 200:
                sent += 1
            elif response.status_code in {400, 410} and reason in self._permanent_token_reasons:
                self._storage.disable_push_token(
                    household_id,
                    registration["id"],
                    datetime.now(UTC),
                )
                disabled += 1
            elif response.status_code == 429 or response.status_code >= 500:
                raise RuntimeError(f"APNs transient failure status={response.status_code}")
            else:
                skipped += 1
                logger.warning(
                    "APNs notification rejected, household_id=%s status=%s reason=%s",
                    household_id,
                    response.status_code,
                    reason or "unknown",
                )

        logger.info(
            "APNs notification completed, household_id=%s attempted=%s sent=%s disabled=%s skipped=%s",
            household_id,
            attempted,
            sent,
            disabled,
            skipped,
        )
        return PushNotificationResult(attempted, sent, disabled, skipped)

    def _send(self, registration: dict, household_id: str) -> httpx.Response:
        environment = registration.get("environment")
        host = (
            "https://api.sandbox.push.apple.com"
            if environment == "sandbox"
            else "https://api.push.apple.com"
        )
        collapse_id = hashlib.sha256(household_id.encode("utf-8")).hexdigest()
        return self._client.post(
            f"{host}/3/device/{registration['token']}",
            headers={
                "authorization": f"bearer {self._authorization_token()}",
                "apns-topic": self._settings.apns_topic,
                "apns-push-type": "background",
                "apns-priority": "5",
                "apns-collapse-id": collapse_id,
            },
            json={
                "aps": {"content-available": 1},
                "reason": "changesAvailable",
            },
        )

    def _authorization_token(self) -> str:
        with self._lock:
            now = time.time()
            if self._bearer_token and now - self._bearer_created_at < 50 * 60:
                return self._bearer_token
            private_key = self._settings.apns_private_key.replace("\\n", "\n")
            token = jwt.encode(
                {"iss": self._settings.apns_team_id, "iat": int(now)},
                private_key,
                algorithm="ES256",
                headers={"kid": self._settings.apns_key_id},
            )
            self._bearer_token = token
            self._bearer_created_at = now
            return token

    @staticmethod
    def _response_reason(response: httpx.Response) -> str | None:
        try:
            value = response.json()
        except ValueError:
            return None
        reason = value.get("reason") if isinstance(value, dict) else None
        return reason if isinstance(reason, str) else None
