from __future__ import annotations

from datetime import UTC, date, datetime
import logging
from typing import Callable
from uuid import UUID

from app.auth import AuthenticatedIdentity
from app.models import (
    AgentAPIKeyCreated,
    AgentAPIKeyView,
    AgentAssetUpdate,
    AgentLedgerBatchCreate,
    AgentLedgerBatchCreateResponse,
    AgentEntityCreateResponse,
    AgentEntityListResponse,
    AgentEntityView,
    AgentLedgerCreateResponse,
    AgentLedgerEntryCreate,
    AgentPrincipal,
    AgentScope,
    Actor,
    ActorType,
    AuditListResponse,
    BootstrapResponse,
    DeviceRegistration,
    PushTokenRegistration,
    MigrationResponse,
    MigrationUploadRequest,
    MutationStatus,
    SyncPullResponse,
    SyncPushRequest,
    SyncPushResponse,
    LedgerEntryCreate,
)
from app.storage.protocols import HouseholdStorage
from app.services.agent_access import AgentAccessService


logger = logging.getLogger(__name__)


class MembershipRequiredError(RuntimeError):
    pass


class WorkspaceNotActiveError(RuntimeError):
    pass


class DeviceRegistrationRequiredError(RuntimeError):
    pass


class CloudService:
    def __init__(
        self,
        storage: HouseholdStorage,
        *,
        change_notifier: Callable[[str], object] | None = None,
    ):
        self._storage = storage
        self._change_notifier = change_notifier

    def bootstrap(
        self,
        identity: AuthenticatedIdentity,
        registration: DeviceRegistration,
    ) -> BootstrapResponse:
        return self._storage.bootstrap_owner(identity.uid, registration)

    def register_push_token(
        self,
        identity: AuthenticatedIdentity,
        registration: PushTokenRegistration,
    ) -> None:
        household_id = self._required_household(identity.uid)
        device = self._storage.read_household_document(
            household_id, str(registration.device_id)
        )
        if device is None or device.get("entityType") != "device":
            raise DeviceRegistrationRequiredError("Bootstrap this device first")
        self._storage.upsert_push_token(
            household_id,
            identity.uid,
            registration,
            datetime.now(UTC),
        )

    def delete_account(self, identity: AuthenticatedIdentity) -> int:
        """Erase the owner identity membership and every household document.

        The operation is intentionally idempotent so a client can safely retry
        after transport loss without recreating or retaining account data.
        """
        return self._storage.delete_account_data(identity.uid)

    def create_agent_api_key(
        self,
        identity: AuthenticatedIdentity,
        access: AgentAccessService,
    ) -> AgentAPIKeyCreated:
        household_id = self._required_household(identity.uid)
        self._require_active_household(household_id)
        return access.create_api_key(household_id, identity.uid)

    def agent_api_key(
        self,
        identity: AuthenticatedIdentity,
    ) -> AgentAPIKeyView | None:
        household_id = self._required_household(identity.uid)
        self._require_active_household(household_id)
        return self._storage.agent_api_key(household_id)

    def revoke_agent_api_key(
        self,
        identity: AuthenticatedIdentity,
    ) -> AgentAPIKeyView | None:
        household_id = self._required_household(identity.uid)
        self._require_active_household(household_id)
        return self._storage.revoke_agent_api_key(
            household_id,
            Actor(type=ActorType.user, id=identity.uid),
            datetime.now(UTC),
        )

    def agent_create_ledger_entry(
        self,
        principal: AgentPrincipal,
        request: AgentLedgerEntryCreate,
    ) -> AgentLedgerCreateResponse:
        self._require_active_household(str(principal.household_id))
        self._require_agent_scope(
            principal, AgentScope.ledger_create, "ledger.create"
        )
        storage_request = LedgerEntryCreate(
            **request.model_dump(),
            household_id=principal.household_id,
            source=principal.integration,
        )
        result = self._storage.create_ledger_entry(
            storage_request,
            Actor(type=ActorType.agent, id=str(principal.connection_id)),
        )
        self._notify_changes(str(principal.household_id))
        return AgentLedgerCreateResponse(entry=result.entry, replayed=result.replayed)

    def agent_create_ledger_batch(
        self,
        principal: AgentPrincipal,
        request: AgentLedgerBatchCreate,
    ) -> AgentLedgerBatchCreateResponse:
        self._require_active_household(str(principal.household_id))
        self._require_agent_scope(
            principal, AgentScope.ledger_create, "ledger.create.batch"
        )
        actor = Actor(type=ActorType.agent, id=str(principal.connection_id))
        results = []
        for entry in request.entries:
            storage_request = LedgerEntryCreate(
                **entry.model_dump(),
                household_id=principal.household_id,
                source=principal.integration,
            )
            result = self._storage.create_ledger_entry(storage_request, actor)
            results.append(
                AgentLedgerCreateResponse(
                    entry=result.entry,
                    replayed=result.replayed,
                )
            )
        self._notify_changes(str(principal.household_id))
        replayed_count = sum(result.replayed for result in results)
        return AgentLedgerBatchCreateResponse(
            results=results,
            created_count=len(results) - replayed_count,
            replayed_count=replayed_count,
        )

    def agent_list_ledger_entries(
        self,
        principal: AgentPrincipal,
        limit: int,
        cursor: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> AgentEntityListResponse:
        return self._agent_list(
            principal,
            AgentScope.ledger_read,
            {"ledgerEntry"},
            "ledger.read",
            limit,
            cursor=cursor,
            start_date=start_date,
            end_date=end_date,
            temporal_field="occurredAt",
        )

    def agent_list_assets(
        self,
        principal: AgentPrincipal,
        limit: int,
        cursor: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> AgentEntityListResponse:
        return self._agent_list(
            principal,
            AgentScope.assets_read,
            {"assetAccount", "assetSnapshot"},
            "assets.read",
            limit,
            cursor=cursor,
            start_date=start_date,
            end_date=end_date,
            temporal_field="observedAt",
            always_include_entity_types={"assetAccount"},
        )

    def agent_list_categories(
        self,
        principal: AgentPrincipal,
        limit: int,
    ) -> AgentEntityListResponse:
        return self._agent_list(
            principal,
            AgentScope.categories_read,
            {"category"},
            "categories.read",
            limit,
        )

    def agent_list_channels(
        self,
        principal: AgentPrincipal,
        limit: int,
    ) -> AgentEntityListResponse:
        return self._agent_list(
            principal,
            AgentScope.channels_read,
            {"paymentChannel"},
            "channels.read",
            limit,
        )

    def agent_update_asset(
        self,
        principal: AgentPrincipal,
        account_id: UUID,
        request: AgentAssetUpdate,
    ) -> AgentEntityCreateResponse:
        self._require_active_household(str(principal.household_id))
        self._require_agent_scope(
            principal, AgentScope.assets_update, "assets.update"
        )
        account = self._storage.read_household_document(
            str(principal.household_id), str(account_id)
        )
        if (
            account is None
            or account.get("entityType") != "assetAccount"
            or account.get("deletedAt") is not None
        ):
            raise ValueError("Asset account not found")
        actor = Actor(type=ActorType.agent, id=str(principal.connection_id))
        payload = request.model_dump(
            by_alias=True,
            mode="json",
            exclude={"snapshot_id", "idempotency_key"},
        )
        payload["accountId"] = str(account_id)
        before = self._asset_balance_before(str(principal.household_id), account)
        after = {
            "entityType": "assetSnapshot",
            "entityId": str(request.snapshot_id),
            "revision": 1,
            "accountId": str(account_id),
            "amountInFen": request.amount_in_fen,
            "observedAt": request.observed_at.isoformat().replace("+00:00", "Z"),
            "memberProfileId": request.member_profile_id,
        }
        now = datetime.now(UTC)
        related_update = None
        if self._should_materialize_asset_balance(before, request.observed_at):
            related_update = dict(account)
            related_payload = dict(account.get("payload") or account)
            related_payload["amountInFen"] = request.amount_in_fen
            related_payload["balanceObservedAt"] = request.observed_at.isoformat().replace(
                "+00:00", "Z"
            )
            related_update["payload"] = related_payload
            related_update["revision"] = int(account.get("revision", 0)) + 1
            related_update["updatedAt"] = now.isoformat().replace("+00:00", "Z")
            related_update["actor"] = actor.model_dump(mode="json")
            related_update["operationId"] = str(request.idempotency_key)
            related_update["lastAcceptedMutationId"] = str(request.idempotency_key)
        document, replayed = self._storage.create_agent_entity(
            str(principal.household_id),
            actor,
            "assetSnapshot",
            str(request.snapshot_id),
            str(request.idempotency_key),
            AgentScope.assets_update.value,
            "assets.update",
            principal.integration.value,
            payload,
            {"before": before, "after": after},
            now,
            related_update=related_update,
        )
        self._notify_changes(str(principal.household_id))
        return AgentEntityCreateResponse(
            item=self._agent_entity_view(document),
            replayed=replayed,
        )

    @staticmethod
    def _should_materialize_asset_balance(before: dict, observed_at: datetime) -> bool:
        prior_text = before.get("observedAt")
        if not isinstance(prior_text, str):
            return True
        try:
            prior = datetime.fromisoformat(prior_text.replace("Z", "+00:00"))
        except ValueError:
            return True
        return observed_at >= prior

    def _asset_balance_before(self, household_id: str, account: dict) -> dict:
        snapshots = self._storage.list_agent_entities(
            household_id, {"assetSnapshot"}, 500
        )
        matching = [
            item
            for item in snapshots
            if (item.get("payload") or {}).get("accountId") == account["id"]
        ]
        if matching:
            latest = max(
                matching,
                key=lambda item: (
                    (item.get("payload") or {}).get("observedAt", ""),
                    item.get("updatedAt", ""),
                ),
            )
            latest_payload = latest.get("payload") or {}
            return {
                "entityType": "assetSnapshot",
                "entityId": latest["id"],
                "revision": latest.get("revision"),
                "accountId": account["id"],
                "amountInFen": latest_payload.get("amountInFen"),
                "observedAt": latest_payload.get("observedAt"),
                "memberProfileId": latest_payload.get("memberProfileId"),
            }
        account_payload = account.get("payload") or account
        return {
            "entityType": "assetAccount",
            "entityId": account["id"],
            "revision": account.get("revision"),
            "accountId": account["id"],
            "amountInFen": account_payload.get("amountInFen"),
        }

    def push(
        self,
        identity: AuthenticatedIdentity,
        request: SyncPushRequest,
    ) -> SyncPushResponse:
        household_id = self._required_household(identity.uid)
        self._require_active_household(household_id)
        actor = Actor(type=ActorType.user, id=identity.uid)
        results = []
        blocked = False
        for mutation in request.mutations:
            if blocked:
                break
            result = self._storage.push_mutation(household_id, actor, mutation)
            results.append(result)
            blocked = result.status in {MutationStatus.conflict, MutationStatus.rejected}
        if any(
            result.status in {MutationStatus.accepted, MutationStatus.replayed}
            for result in results
        ):
            self._notify_changes(household_id)
        return SyncPushResponse(results=results)

    def pull(
        self,
        identity: AuthenticatedIdentity,
        cursor: str | None,
        limit: int,
    ) -> SyncPullResponse:
        household_id = self._required_household(identity.uid)
        changes, next_cursor, has_more = self._storage.pull_changes(
            household_id,
            cursor,
            limit,
        )
        return SyncPullResponse(
            changes=changes,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def audit(
        self,
        identity: AuthenticatedIdentity,
        cursor: str | None,
        limit: int,
    ) -> AuditListResponse:
        household_id = self._required_household(identity.uid)
        events, next_cursor, has_more = self._storage.list_audit_events(
            household_id,
            cursor,
            limit,
        )
        return AuditListResponse(events=events, next_cursor=next_cursor, has_more=has_more)

    def stage_migration(
        self,
        identity: AuthenticatedIdentity,
        request: MigrationUploadRequest,
    ) -> MigrationResponse:
        household_id = self._required_household(identity.uid)
        actor = Actor(type=ActorType.user, id=identity.uid)
        return self._storage.stage_migration(household_id, actor, request)

    def activate_migration(
        self,
        identity: AuthenticatedIdentity,
        session_id: UUID,
        content_digest: str,
    ) -> MigrationResponse:
        household_id = self._required_household(identity.uid)
        actor = Actor(type=ActorType.user, id=identity.uid)
        return self._storage.activate_migration(
            household_id,
            actor,
            str(session_id),
            content_digest,
        )

    def _required_household(self, uid: str) -> str:
        household_id = self._storage.household_for_uid(uid)
        if household_id is None:
            raise MembershipRequiredError("Bootstrap is required")
        return household_id

    def _notify_changes(self, household_id: str) -> None:
        """Best-effort fast path; Cosmos Change Feed remains the durable fallback."""
        if self._change_notifier is None:
            return
        try:
            self._change_notifier(household_id)
        except Exception:
            logger.exception(
                "Immediate APNs notification failed, household_id=%s",
                household_id,
            )

    def _require_active_household(self, household_id: str) -> None:
        household = self._storage.read_household_document(household_id, household_id)
        if household is None or household.get("status") != "active":
            raise WorkspaceNotActiveError(
                "Agent Cloud workspace must complete migration activation before normal writes"
            )

    def _agent_list(
        self,
        principal: AgentPrincipal,
        required_scope: AgentScope,
        entity_types: set[str],
        action: str,
        limit: int,
        *,
        cursor: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        temporal_field: str | None = None,
        always_include_entity_types: set[str] | None = None,
    ) -> AgentEntityListResponse:
        self._require_active_household(str(principal.household_id))
        self._require_agent_scope(principal, required_scope, action)
        if start_date is not None and end_date is not None and start_date > end_date:
            raise ValueError("startDate must not be after endDate")
        household_id = str(principal.household_id)
        actor = Actor(type=ActorType.agent, id=str(principal.connection_id))
        if temporal_field is None and cursor is None:
            documents = self._storage.list_agent_entities(
                household_id, entity_types, limit
            )
            next_cursor = None
            has_more = False
        else:
            documents, next_cursor, has_more = self._storage.list_agent_entities_page(
                household_id,
                entity_types,
                limit,
                cursor,
                start_date,
                end_date,
                temporal_field,
                always_include_entity_types or set(),
            )
        self._storage.record_agent_read(
            household_id,
            actor,
            required_scope.value,
            action,
            len(documents),
            datetime.now(UTC),
        )
        return AgentEntityListResponse(
            items=[self._agent_entity_view(document) for document in documents],
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def _require_agent_scope(
        self,
        principal: AgentPrincipal,
        required_scope: AgentScope,
        action: str,
    ) -> None:
        if required_scope in principal.scopes:
            return
        actor = Actor(type=ActorType.agent, id=str(principal.connection_id))
        self._storage.record_agent_denial(
            str(principal.household_id),
            actor,
            required_scope.value,
            action,
            datetime.now(UTC),
        )
        raise PermissionError(f"Missing {required_scope.value} scope")

    @staticmethod
    def _agent_entity_view(document: dict) -> AgentEntityView:
        payload = document.get("payload")
        if payload is None:
            fields = {
                "ledgerEntry": (
                    "kind", "direction", "occurredAt", "monthStart", "channelId",
                    "categoryId", "amountInFen", "note", "memberProfileId",
                ),
                "assetAccount": ("name", "amountInFen"),
                "assetSnapshot": (
                    "accountId", "memberProfileId", "amountInFen", "observedAt",
                ),
                "paymentChannel": (
                    "name", "symbolName", "assetName", "sortOrder", "isArchived", "isSystem",
                ),
                "category": (
                    "name", "symbolName", "sortOrder", "isArchived", "isSystem", "direction",
                    "scope", "assetGroup",
                ),
            }.get(document["entityType"], ())
            payload = {key: document.get(key) for key in fields}
        return AgentEntityView(
            entity_type=document["entityType"],
            entity_id=document["id"],
            revision=document["revision"],
            created_at=document["createdAt"],
            updated_at=document["updatedAt"],
            payload=payload,
        )
