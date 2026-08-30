from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from typing import Any

from app.auth import AuthenticatedIdentity
from app.models import (
    AdminAppleSubscriptionView,
    AdminAuditEventView,
    AdminAuditListResponse,
    AdminEntitlementSummary,
    AdminGrantCreateRequest,
    AdminGrantMutationResponse,
    AdminGrantRevokeRequest,
    AdminGrantType,
    AdminManualGrantListItem,
    AdminManualGrantListResponse,
    AdminManualGrantView,
    AdminOverviewResponse,
    AdminUserDetail,
    AdminUserEntitlementResponse,
    AdminUserListResponse,
    AdminUserStatus,
    AdminUserSummary,
)
from app.services.entitlements import EntitlementResolver
from app.storage.protocols import HouseholdStorage


class AdminServiceError(RuntimeError):
    pass


class AdminTargetNotFoundError(AdminServiceError):
    pass


class AdminTargetNotReadyError(AdminServiceError):
    pass


class AdminGrantNotFoundError(AdminServiceError):
    pass


class AdminIdempotencyConflictError(AdminServiceError):
    pass


class AdminGrantAlreadyRevokedError(AdminServiceError):
    pass


class AdminInvalidGrantPeriodError(AdminServiceError):
    pass


class AdminService:
    """Narrow, PII-minimized operator surface for Pro entitlement support."""

    def __init__(self, storage: HouseholdStorage):
        self._storage = storage
        self._entitlements = EntitlementResolver(storage)

    def overview(self, now: datetime | None = None) -> AdminOverviewResponse:
        generated_at = (now or datetime.now(UTC)).astimezone(UTC)
        counts = self._storage.admin_overview_counts(generated_at)
        audits, _, _ = self._storage.list_admin_audit(
            uid=None,
            action=None,
            outcome=None,
            from_at=generated_at - timedelta(days=30),
            to_at=None,
            limit=8,
            cursor=None,
        )
        return AdminOverviewResponse(
            active_pro_accounts=counts["activeProAccounts"],
            active_manual_grant_accounts=counts["activeManualGrantAccounts"],
            manual_grants_expiring_within_days=counts["manualGrantsExpiringWithinDays"],
            recent_admin_actions=len(audits),
            generated_at=generated_at,
        )

    def list_users(
        self,
        query: str,
        status: AdminUserStatus,
        limit: int,
        cursor: str | None,
    ) -> AdminUserListResponse:
        normalized = query.strip().casefold()
        items: list[AdminUserSummary] = []
        raw_cursor = cursor
        has_more = True
        while len(items) < limit and has_more:
            memberships, raw_cursor, has_more = self._storage.list_identity_memberships(
                query, limit, raw_cursor
            )
            for membership in memberships:
                summary = self._summary(membership)
                sources = set(summary.effective_entitlement.sources)
                if status is AdminUserStatus.pro and not summary.effective_entitlement.active:
                    continue
                if status is AdminUserStatus.free and summary.effective_entitlement.active:
                    continue
                if status is AdminUserStatus.manual_grant and "manualGrant" not in sources:
                    continue
                items.append(summary)
                if len(items) >= limit:
                    break
            if not memberships:
                break
        items.sort(key=lambda item: self._search_rank(item, normalized))
        return AdminUserListResponse(items=items[:limit], next_cursor=raw_cursor if has_more else None)

    def user_detail(self, uid: str) -> AdminUserDetail:
        membership = self._require_identity(uid)
        summary = self._summary(membership)
        return AdminUserDetail(
            **summary.model_dump(),
            household_ready=bool(membership.get("householdId")),
        )

    def entitlement_detail(self, uid: str) -> AdminUserEntitlementResponse:
        self._require_identity(uid)
        now = datetime.now(UTC)
        effective = self._entitlements.resolve(uid, now)
        apples = [self._apple_view(document, now) for document in effective.apple_documents]
        grants = [self._grant_view(document) for document in effective.manual_documents]
        return AdminUserEntitlementResponse(
            uid=uid,
            effective=self._effective_summary(effective),
            apple_subscriptions=apples,
            manual_grants=grants,
        )

    def list_manual_grants(
        self,
        *,
        status: str | None,
        expiring_within_days: int | None,
        limit: int,
        cursor: str | None,
        now: datetime | None = None,
    ) -> AdminManualGrantListResponse:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        items: list[AdminManualGrantListItem] = []
        cutoff = current + timedelta(days=expiring_within_days) if expiring_within_days else None
        raw_cursor = cursor
        has_more = True
        while len(items) < limit and has_more:
            records, raw_cursor, has_more = self._storage.list_manual_pro_grants(limit, raw_cursor)
            for record in records:
                active = self._is_active_manual(record, current)
                if status == "active" and not active:
                    continue
                if status == "expired" and (active or record.get("revokedAt")):
                    continue
                if status == "revoked" and not record.get("revokedAt"):
                    continue
                expires_at = self._parse_datetime(record.get("expiresAt"))
                if cutoff is not None and (not active or expires_at is None or expires_at > cutoff):
                    continue
                membership = self._storage.identity_membership(record["uid"]) or {}
                items.append(AdminManualGrantListItem(
                    **self._grant_view(record).model_dump(),
                    display_name=membership.get("displayName"),
                    email=membership.get("email"),
                    active=active,
                ))
                if len(items) >= limit:
                    break
            if not records:
                break
        return AdminManualGrantListResponse(
            items=items,
            next_cursor=raw_cursor if has_more else None,
        )

    def create_manual_grant(
        self,
        admin: AuthenticatedIdentity,
        uid: str,
        request: AdminGrantCreateRequest,
        idempotency_key: str,
        request_id: str | None,
        now: datetime | None = None,
    ) -> AdminGrantMutationResponse:
        membership = self._require_identity(uid)
        household_id = membership.get("householdId")
        if not isinstance(household_id, str) or not household_id:
            raise AdminTargetNotReadyError("Anke identity is not ready")
        created_at = self._parse_datetime(membership.get("createdAt"))
        if created_at is not None and request.starts_at < created_at:
            raise AdminInvalidGrantPeriodError("startsAt cannot predate account creation")
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        request_hash = self._request_hash(
            "manualProGrant.create",
            uid,
            request.model_dump(by_alias=True, mode="json"),
        )
        grant_id = f"pro-grant:{idempotency_key}"
        existing = self._storage.manual_pro_grant(uid, grant_id)
        if existing is not None:
            if existing.get("requestHash") != request_hash:
                raise AdminIdempotencyConflictError("Idempotency key was already used for a different write")
            return AdminGrantMutationResponse(
                grant=self._grant_view(existing),
                effective_entitlement=self._effective_summary(self._entitlements.resolve(uid, timestamp)),
                replayed=True,
            )
        document = {
            "id": grant_id,
            "entityType": "manualProGrant",
            "uid": uid,
            "householdId": household_id,
            "source": "manual",
            "grantType": request.grant_type.value,
            "startsAt": self._timestamp(request.starts_at),
            "expiresAt": self._timestamp(request.expires_at) if request.expires_at else None,
            "revokedAt": None,
            "reason": request.reason,
            "createdBy": admin.uid,
            "createdAt": self._timestamp(timestamp),
            "updatedAt": self._timestamp(timestamp),
            "requestHash": request_hash,
        }
        stored = self._storage.upsert_manual_pro_grant(document)
        self._storage.append_admin_audit(self._audit_document(
            audit_id=f"admin-audit:create:{idempotency_key}",
            action="manualProGrant.create",
            admin=admin,
            uid=uid,
            grant_id=grant_id,
            reason=request.reason,
            request_id=request_id,
            created_at=timestamp,
        ))
        return AdminGrantMutationResponse(
            grant=self._grant_view(stored),
            effective_entitlement=self._effective_summary(self._entitlements.resolve(uid, timestamp)),
            replayed=False,
        )

    def revoke_manual_grant(
        self,
        admin: AuthenticatedIdentity,
        uid: str,
        grant_id: str,
        request: AdminGrantRevokeRequest,
        idempotency_key: str,
        request_id: str | None,
        now: datetime | None = None,
    ) -> AdminGrantMutationResponse:
        self._require_identity(uid)
        existing = self._storage.manual_pro_grant(uid, grant_id)
        if existing is None:
            raise AdminGrantNotFoundError("Manual grant not found")
        request_hash = self._request_hash(
            "manualProGrant.revoke",
            uid,
            {"grantId": grant_id, "reason": request.reason},
        )
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        if existing.get("revokedAt"):
            if existing.get("revokeRequestHash") != request_hash:
                raise AdminGrantAlreadyRevokedError("Manual grant is already revoked")
            return AdminGrantMutationResponse(
                grant=self._grant_view(existing),
                effective_entitlement=self._effective_summary(self._entitlements.resolve(uid, timestamp)),
                replayed=True,
            )
        updated = dict(existing)
        updated.update({
            "revokedAt": self._timestamp(timestamp),
            "revokedBy": admin.uid,
            "revocationReason": request.reason,
            "revokeIdempotencyKey": idempotency_key,
            "revokeRequestHash": request_hash,
            "updatedAt": self._timestamp(timestamp),
        })
        stored = self._storage.upsert_manual_pro_grant(updated)
        self._storage.append_admin_audit(self._audit_document(
            audit_id=f"admin-audit:revoke:{idempotency_key}",
            action="manualProGrant.revoke",
            admin=admin,
            uid=uid,
            grant_id=grant_id,
            reason=request.reason,
            request_id=request_id,
            created_at=timestamp,
        ))
        return AdminGrantMutationResponse(
            grant=self._grant_view(stored),
            effective_entitlement=self._effective_summary(self._entitlements.resolve(uid, timestamp)),
            replayed=False,
        )

    def audit(
        self,
        *,
        uid: str | None,
        action: str | None,
        outcome: str | None,
        from_at: datetime | None,
        to_at: datetime | None,
        limit: int,
        cursor: str | None,
    ) -> AdminAuditListResponse:
        records, next_cursor, has_more = self._storage.list_admin_audit(
            uid=uid,
            action=action,
            outcome=outcome,
            from_at=from_at,
            to_at=to_at,
            limit=limit,
            cursor=cursor,
        )
        return AdminAuditListResponse(
            items=[AdminAuditEventView(
                id=record["id"],
                action=record["action"],
                outcome=record["outcome"],
                target_uid=record["targetUid"],
                grant_id=record.get("grantId"),
                actor_uid=record["actorUid"],
                reason=record.get("reason"),
                request_id=record.get("requestId"),
                created_at=self._parse_datetime(record.get("createdAt")) or datetime.now(UTC),
            ) for record in records],
            next_cursor=next_cursor if has_more else None,
        )

    def _require_identity(self, uid: str) -> dict:
        membership = self._storage.identity_membership(uid)
        if membership is None:
            raise AdminTargetNotFoundError("Account not found")
        return membership

    def _summary(self, membership: dict) -> AdminUserSummary:
        uid = str(membership.get("uid") or membership.get("id"))
        effective = self._entitlements.resolve(uid)
        return AdminUserSummary(
            uid=uid,
            display_name=membership.get("displayName"),
            email=membership.get("email"),
            provider=membership.get("provider", "unknown"),
            created_at=self._parse_datetime(membership.get("createdAt")) or datetime.now(UTC),
            effective_entitlement=self._effective_summary(effective),
        )

    @staticmethod
    def _search_rank(item: AdminUserSummary, query: str) -> tuple[int, str]:
        exact = {item.uid.casefold()}
        if item.email:
            exact.add(item.email.casefold())
        return (0 if query in exact else 1, item.uid)

    @staticmethod
    def _effective_summary(effective) -> AdminEntitlementSummary:
        return AdminEntitlementSummary(
            active=effective.active,
            sources=list(effective.sources),
            expires_at=effective.expires_at,
        )

    @classmethod
    def _is_active_manual(cls, document: dict, now: datetime) -> bool:
        if document.get("revokedAt"):
            return False
        starts_at = cls._parse_datetime(document.get("startsAt"))
        if starts_at is None or starts_at > now:
            return False
        expires_at = cls._parse_datetime(document.get("expiresAt"))
        return expires_at is None or expires_at > now

    def _grant_view(self, document: dict) -> AdminManualGrantView:
        return AdminManualGrantView(
            id=document["id"],
            uid=document["uid"],
            grant_type=document.get("grantType", AdminGrantType.fixed_term.value),
            starts_at=self._parse_datetime(document.get("startsAt")) or datetime.now(UTC),
            expires_at=self._parse_datetime(document.get("expiresAt")),
            revoked_at=self._parse_datetime(document.get("revokedAt")),
            reason=document.get("reason", ""),
            created_by=document.get("createdBy", ""),
            created_at=self._parse_datetime(document.get("createdAt")) or datetime.now(UTC),
            updated_at=self._parse_datetime(document.get("updatedAt")) or datetime.now(UTC),
        )

    @staticmethod
    def _apple_view(document: dict, now: datetime) -> AdminAppleSubscriptionView:
        parse = AdminService._parse_datetime
        starts_at = parse(document.get("startsAt")) or parse(document.get("purchaseDate"))
        expires_at = parse(document.get("expiresAt"))
        revoked_at = parse(document.get("revokedAt"))
        return AdminAppleSubscriptionView(
            product_id=document.get("productId"),
            original_transaction_id=document.get("originalTransactionId"),
            transaction_id=document.get("transactionId"),
            active=bool(document.get("active")) and revoked_at is None and (
                starts_at is None or starts_at <= now
            ) and (expires_at is None or expires_at > now),
            starts_at=starts_at,
            expires_at=expires_at,
            revoked_at=revoked_at,
            environment=document.get("environment"),
        )

    @staticmethod
    def _audit_document(
        *,
        audit_id: str,
        action: str,
        admin: AuthenticatedIdentity,
        uid: str,
        grant_id: str,
        reason: str,
        request_id: str | None,
        created_at: datetime,
    ) -> dict:
        return {
            "id": audit_id,
            "entityType": "adminAuditEvent",
            "uid": uid,
            "targetUid": uid,
            "grantId": grant_id,
            "actorUid": admin.uid,
            "action": action,
            "outcome": "accepted",
            "reason": reason,
            "requestId": request_id,
            "createdAt": AdminService._timestamp(created_at),
        }

    @staticmethod
    def _request_hash(action: str, uid: str, payload: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"action": action, "uid": uid, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _timestamp(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                return None
            return value.astimezone(UTC)
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
