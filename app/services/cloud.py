from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.auth import AuthenticatedIdentity
from app.models import (
    AgentConnectionCreate,
    AgentConnectionCreated,
    AgentConnectionView,
    AgentAssetSnapshotCreate,
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


class MembershipRequiredError(RuntimeError):
    pass


class WorkspaceNotActiveError(RuntimeError):
    pass


class CloudService:
    def __init__(self, storage: HouseholdStorage):
        self._storage = storage

    def bootstrap(
        self,
        identity: AuthenticatedIdentity,
        registration: DeviceRegistration,
    ) -> BootstrapResponse:
        return self._storage.bootstrap_owner(identity.uid, registration)

    def create_agent_connection(
        self,
        identity: AuthenticatedIdentity,
        request: AgentConnectionCreate,
        access: AgentAccessService,
    ) -> AgentConnectionCreated:
        household_id = self._required_household(identity.uid)
        self._require_active_household(household_id)
        return access.create_connection(household_id, identity.uid, request)

    def list_agent_connections(
        self,
        identity: AuthenticatedIdentity,
    ) -> list[AgentConnectionView]:
        household_id = self._required_household(identity.uid)
        self._require_active_household(household_id)
        return self._storage.list_agent_connections(household_id)

    def revoke_agent_connection(
        self,
        identity: AuthenticatedIdentity,
        connection_id: UUID,
    ) -> AgentConnectionView:
        household_id = self._required_household(identity.uid)
        self._require_active_household(household_id)
        return self._storage.revoke_agent_connection(
            household_id,
            Actor(type=ActorType.user, id=identity.uid),
            str(connection_id),
        )

    def agent_create_ledger_entry(
        self,
        principal: AgentPrincipal,
        request: AgentLedgerEntryCreate,
    ) -> AgentLedgerCreateResponse:
        self._require_active_household(str(principal.household_id))
        self._require_agent_scope(
            principal, AgentScope.ledger_entry_create, "ledger.create"
        )
        storage_request = LedgerEntryCreate(
            **request.model_dump(),
            household_id=principal.household_id,
        )
        result = self._storage.create_ledger_entry(
            storage_request,
            Actor(type=ActorType.agent, id=str(principal.connection_id)),
        )
        return AgentLedgerCreateResponse(entry=result.entry, replayed=result.replayed)

    def agent_list_ledger_entries(
        self,
        principal: AgentPrincipal,
        limit: int,
    ) -> AgentEntityListResponse:
        return self._agent_list(
            principal,
            AgentScope.ledger_read,
            {"ledgerEntry"},
            "ledger.read",
            limit,
        )

    def agent_list_assets(
        self,
        principal: AgentPrincipal,
        limit: int,
    ) -> AgentEntityListResponse:
        return self._agent_list(
            principal,
            AgentScope.assets_read,
            {"assetAccount", "assetSnapshot"},
            "assets.read",
            limit,
        )

    def agent_list_reference_data(
        self,
        principal: AgentPrincipal,
        limit: int,
    ) -> AgentEntityListResponse:
        return self._agent_list(
            principal,
            AgentScope.reference_data_read,
            {"paymentChannel", "category"},
            "reference-data.read",
            limit,
        )

    def agent_create_asset_snapshot(
        self,
        principal: AgentPrincipal,
        request: AgentAssetSnapshotCreate,
    ) -> AgentEntityCreateResponse:
        self._require_active_household(str(principal.household_id))
        self._require_agent_scope(
            principal, AgentScope.assets_snapshot_create, "assets.snapshot.create"
        )
        account = self._storage.read_household_document(
            str(principal.household_id), str(request.account_id)
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
            exclude={"id", "operation_id"},
        )
        document, replayed = self._storage.create_agent_entity(
            str(principal.household_id),
            actor,
            "assetSnapshot",
            str(request.id),
            str(request.operation_id),
            AgentScope.assets_snapshot_create.value,
            "assets.snapshot.create",
            payload,
            datetime.now(UTC),
        )
        return AgentEntityCreateResponse(
            item=self._agent_entity_view(document),
            replayed=replayed,
        )

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
    ) -> AgentEntityListResponse:
        self._require_active_household(str(principal.household_id))
        self._require_agent_scope(principal, required_scope, action)
        household_id = str(principal.household_id)
        actor = Actor(type=ActorType.agent, id=str(principal.connection_id))
        documents = self._storage.list_agent_entities(
            household_id, entity_types, limit
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
            items=[self._agent_entity_view(document) for document in documents]
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
