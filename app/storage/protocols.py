from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from app.models import (
    AgentAPIKeyView,
    AgentPrincipal,
    Actor,
    AuditEventView,
    BootstrapResponse,
    DeviceRegistration,
    PushTokenRegistration,
    LedgerEntryCreate,
    LedgerEntryDocument,
    MigrationResponse,
    MigrationUploadRequest,
    MutationResult,
    SyncChange,
    SyncMutation,
)
from app.auth import AuthenticatedIdentity


@dataclass(frozen=True, slots=True)
class LedgerCreateResult:
    entry: LedgerEntryDocument
    replayed: bool


@dataclass(frozen=True, slots=True)
class RetentionResult:
    tombstone_payloads_purged: int
    audit_events_deleted: int


class HouseholdStorage(Protocol):
    def run_retention(self, now: datetime) -> RetentionResult: ...

    def ensure_identity(self, identity: AuthenticatedIdentity) -> None: ...

    def update_identity_profile(
        self,
        identity: AuthenticatedIdentity,
        display_name: str,
    ) -> None: ...

    def delete_account_data(self, uid: str) -> int: ...

    def upsert_subscription_entitlement(self, document: dict) -> dict: ...

    def subscription_entitlement(self, uid: str) -> dict | None: ...

    def subscription_entitlements(self, uid: str) -> list[dict]: ...

    def subscription_by_original_transaction_id(
        self, original_transaction_id: str
    ) -> dict | None: ...

    def has_active_pro_entitlement(self, household_id: str) -> bool: ...

    def identity_membership(self, uid: str) -> dict | None: ...

    def list_identity_memberships(
        self,
        query: str,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[dict], str | None, bool]: ...

    def admin_overview_counts(self, now: datetime) -> dict[str, int]: ...

    def manual_pro_grants(self, uid: str) -> list[dict]: ...

    def manual_pro_grant(self, uid: str, grant_id: str) -> dict | None: ...

    def list_manual_pro_grants(
        self,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[dict], str | None, bool]: ...

    def upsert_manual_pro_grant(self, document: dict) -> dict: ...

    def append_admin_audit(self, document: dict) -> dict: ...

    def list_admin_audit(
        self,
        *,
        uid: str | None,
        action: str | None,
        outcome: str | None,
        from_at: datetime | None,
        to_at: datetime | None,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[dict], str | None, bool]: ...

    def replace_agent_api_key(
        self,
        household_id: str,
        actor: Actor,
        connection_id: str,
        key_hash: str,
        key_prefix: str,
        now: datetime,
    ) -> AgentAPIKeyView: ...

    def agent_api_key(
        self,
        household_id: str,
    ) -> AgentAPIKeyView | None: ...

    def revoke_agent_api_key(
        self,
        household_id: str,
        actor: Actor,
        now: datetime,
    ) -> AgentAPIKeyView | None: ...

    def consume_agent_request(
        self,
        household_id: str,
        connection_id: str,
        now: datetime,
        limit: int,
        window_seconds: int,
    ) -> bool: ...

    def authenticate_agent_api_key(
        self,
        household_id: str,
        connection_id: str,
        key_hash: str,
        now,
    ) -> AgentPrincipal | None: ...

    def record_agent_auth_failure(
        self,
        household_id: str,
        connection_id: str,
        reason: str,
        now,
        threshold: int,
        window_seconds: int,
    ) -> None: ...

    def list_agent_entities(
        self,
        household_id: str,
        entity_types: set[str],
        limit: int,
    ) -> list[dict]: ...

    def list_agent_entities_page(
        self,
        household_id: str,
        entity_types: set[str],
        limit: int,
        cursor: str | None,
        start_date: date | None,
        end_date: date | None,
        temporal_field: str,
        always_include_entity_types: set[str],
    ) -> tuple[list[dict], str | None, bool]: ...

    def create_agent_entity(
        self,
        household_id: str,
        actor: Actor,
        entity_type: str,
        entity_id: str,
        idempotency_key: str,
        scope: str,
        action: str,
        source: str,
        payload: dict,
        change_summary: dict,
        now: datetime,
        related_update: dict | None = None,
        related_creates: list[dict] | None = None,
    ) -> tuple[dict, bool]: ...

    def record_agent_read(
        self,
        household_id: str,
        actor: Actor,
        scope: str,
        action: str,
        result_count: int,
        now: datetime,
    ) -> None: ...

    def record_agent_denial(
        self,
        household_id: str,
        actor: Actor,
        required_scope: str,
        action: str,
        now: datetime,
    ) -> None: ...

    def bootstrap_owner(
        self,
        uid: str,
        registration: DeviceRegistration,
    ) -> BootstrapResponse: ...

    def upsert_push_token(
        self,
        household_id: str,
        owner_uid: str,
        registration: PushTokenRegistration,
        now: datetime,
    ) -> None: ...

    def active_push_tokens(self, household_id: str) -> list[dict]: ...

    def disable_push_token(
        self,
        household_id: str,
        token_document_id: str,
        now: datetime,
    ) -> None: ...

    def household_for_uid(self, uid: str) -> str | None: ...

    def push_mutation(
        self,
        household_id: str,
        actor: Actor,
        mutation: SyncMutation,
    ) -> MutationResult: ...

    def pull_changes(
        self,
        household_id: str,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[SyncChange], str | None, bool]: ...

    def list_audit_events(
        self,
        household_id: str,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[AuditEventView], str | None, bool]: ...

    def stage_migration(
        self,
        household_id: str,
        actor: Actor,
        request: MigrationUploadRequest,
    ) -> MigrationResponse: ...

    def activate_migration(
        self,
        household_id: str,
        actor: Actor,
        session_id: str,
        content_digest: str,
    ) -> MigrationResponse: ...

    def create_ledger_entry(
        self,
        request: LedgerEntryCreate,
        actor: Actor,
    ) -> LedgerCreateResult: ...

    def read_household_document(
        self,
        household_id: str,
        item_id: str,
    ) -> dict | None: ...
