from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.models import (
    AgentConnectionCreate,
    AgentConnectionView,
    AgentPrincipal,
    Actor,
    AuditEventView,
    BootstrapResponse,
    DeviceRegistration,
    LedgerEntryCreate,
    LedgerEntryDocument,
    MigrationResponse,
    MigrationUploadRequest,
    MutationResult,
    SyncChange,
    SyncMutation,
)


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

    def create_agent_connection(
        self,
        household_id: str,
        actor: Actor,
        request: AgentConnectionCreate,
        connection_id: str,
        token_hash: str,
        refresh_token_hash: str,
        token_expires_at,
        now,
    ) -> AgentConnectionView: ...

    def list_agent_connections(
        self,
        household_id: str,
    ) -> list[AgentConnectionView]: ...

    def revoke_agent_connection(
        self,
        household_id: str,
        actor: Actor,
        connection_id: str,
    ) -> AgentConnectionView: ...

    def pause_agent_connection(
        self,
        household_id: str,
        actor: Actor,
        connection_id: str,
        now: datetime,
    ) -> AgentConnectionView: ...

    def resume_agent_connection(
        self,
        household_id: str,
        actor: Actor,
        connection_id: str,
        now: datetime,
    ) -> AgentConnectionView: ...

    def consume_agent_request(
        self,
        household_id: str,
        connection_id: str,
        now: datetime,
        limit: int,
        window_seconds: int,
    ) -> bool: ...

    def authenticate_agent_token(
        self,
        household_id: str,
        connection_id: str,
        token_hash: str,
        now,
    ) -> AgentPrincipal | None: ...

    def refresh_agent_token(
        self,
        household_id: str,
        connection_id: str,
        refresh_token_hash: str,
        new_token_hash: str,
        requested_expires_at,
        now,
    ) -> tuple[AgentPrincipal, datetime] | None: ...

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
