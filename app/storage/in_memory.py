from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime
from datetime import timedelta
import hashlib
import json
from threading import RLock
from uuid import NAMESPACE_URL, UUID, uuid5

from app.auth import AuthenticatedIdentity
from app.models import (
    Actor,
    ActorType,
    AgentAPIKeyView,
    AgentPrincipal,
    AgentScope,
    OperationSource,
    AuditEventView,
    BootstrapResponse,
    DeviceRegistration,
    PushTokenRegistration,
    LedgerEntryCreate,
    LedgerEntryDocument,
    MigrationResponse,
    MigrationStatus,
    MigrationUploadRequest,
    MutationAction,
    MutationResult,
    MutationStatus,
    SyncChange,
    SyncEntityType,
    SyncMutation,
    build_ledger_transaction_documents,
    canonical_write_hash,
)
from app.storage.protocols import LedgerCreateResult, RetentionResult


class InMemoryHouseholdStorage:
    """Credential-free fake that mirrors household partition and idempotency rules."""

    def __init__(self):
        self._items: dict[tuple[str, str], dict] = {}
        self._identities: dict[str, str] = {}
        self._identity_documents: dict[str, dict] = {}
        self._changes: dict[str, list[dict]] = {}
        self._device_sequences: dict[tuple[str, str], int] = {}
        self._lock = RLock()

    def bootstrap_owner(
        self,
        uid: str,
        registration: DeviceRegistration,
    ) -> BootstrapResponse:
        with self._lock:
            if uid not in self._identities:
                self.ensure_identity(
                    AuthenticatedIdentity(uid=uid, provider="unknown", provider_subject=uid)
                )
            household_id = self._identities[uid]
            user_id = str(uuid5(NAMESPACE_URL, f"anke-user:{uid}"))

            device_id = str(registration.device_id)
            connection_id = str(uuid5(NAMESPACE_URL, f"anke-connection:{uid}:{device_id}"))
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            device = {
                "id": device_id,
                "entityType": "device",
                "householdId": household_id,
                "name": registration.name,
                "platform": registration.platform,
                "appVersion": registration.app_version,
                "ownerUserId": user_id,
                "schemaVersion": 1,
                "revision": 1,
                "createdAt": now,
                "updatedAt": now,
            }
            connection = {
                "id": connection_id,
                "entityType": "connection",
                "householdId": household_id,
                "userId": user_id,
                "deviceId": device_id,
                "status": "active",
                "schemaVersion": 1,
                "revision": 1,
                "createdAt": now,
                "updatedAt": now,
            }
            self._items[(household_id, device_id)] = device
            self._items[(household_id, connection_id)] = connection
            household = self._items[(household_id, household_id)]
            return BootstrapResponse(
                user_id=user_id,
                household_id=UUID(household_id),
                device_id=registration.device_id,
                connection_id=UUID(connection_id),
                sync_cursor="0",
                next_outbox_sequence=self._device_sequences.get(
                    (household_id, device_id), 0
                ) + 1,
                workspace_status=household["status"],
            )

    def upsert_push_token(
        self,
        household_id: str,
        owner_uid: str,
        registration: PushTokenRegistration,
        now: datetime,
    ) -> None:
        with self._lock:
            document_id = f"push-device:{registration.device_id}"
            existing = self._items.get((household_id, document_id))
            timestamp = now.isoformat().replace("+00:00", "Z")
            self._items[(household_id, document_id)] = {
                "id": document_id,
                "entityType": "pushDevice",
                "householdId": household_id,
                "deviceId": str(registration.device_id),
                "ownerUid": owner_uid,
                "token": registration.token,
                "environment": registration.environment,
                "topic": registration.topic,
                "appVersion": registration.app_version,
                "disabledAt": None,
                "schemaVersion": 1,
                "revision": int((existing or {}).get("revision", 0)) + 1,
                "createdAt": (existing or {}).get("createdAt", timestamp),
                "updatedAt": timestamp,
            }

    def active_push_tokens(self, household_id: str) -> list[dict]:
        with self._lock:
            return [
                dict(document)
                for (partition, _), document in self._items.items()
                if partition == household_id
                and document.get("entityType") == "pushDevice"
                and document.get("disabledAt") is None
            ]

    def disable_push_token(
        self,
        household_id: str,
        token_document_id: str,
        now: datetime,
    ) -> None:
        with self._lock:
            document = self._items.get((household_id, token_document_id))
            if document is None or document.get("entityType") != "pushDevice":
                return
            document["disabledAt"] = now.isoformat().replace("+00:00", "Z")
            document["updatedAt"] = document["disabledAt"]
            document["revision"] = int(document.get("revision", 0)) + 1

    def ensure_identity(self, identity: AuthenticatedIdentity) -> None:
        with self._lock:
            uid = identity.uid
            existing = self._identity_documents.get(uid)
            if existing is not None:
                if (
                    existing.get("provider") != identity.provider
                    or existing.get("providerSubject") != identity.provider_subject
                ):
                    raise ValueError("Identity provider subject does not match")
                self._update_identity_document(identity)
                return

            household_id = str(uuid5(NAMESPACE_URL, f"anke-household:{uid}"))
            self._identities[uid] = household_id
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            user_id = str(uuid5(NAMESPACE_URL, f"anke-user:{uid}"))
            for document in (
                {
                    "id": user_id,
                    "entityType": "user",
                    "householdId": household_id,
                    "userID": user_id,
                    "authProvider": identity.provider,
                    "providerSubject": identity.provider_subject,
                    "email": identity.email,
                    "displayName": identity.display_name,
                    "role": "owner",
                },
                {
                    "id": household_id,
                    "entityType": "household",
                    "householdId": household_id,
                    "status": "empty",
                    "storageMode": "agentCloud",
                    "lastChangeSequence": 0,
                },
            ):
                document.update({"schemaVersion": 1, "revision": 1, "createdAt": now, "updatedAt": now})
                self._items[(household_id, document["id"])] = document
            membership = {
                "id": uid,
                "uid": uid,
                "entityType": "identityMembership",
                "provider": identity.provider,
                "providerSubject": identity.provider_subject,
                "householdId": household_id,
                "userId": user_id,
                "role": "owner",
                "createdAt": now,
            }
            self._identity_documents[uid] = membership

    def update_identity_profile(
        self,
        identity: AuthenticatedIdentity,
        display_name: str,
    ) -> None:
        with self._lock:
            self.ensure_identity(identity)
            household_id = self._identities[identity.uid]
            user_id = str(uuid5(NAMESPACE_URL, f"anke-user:{identity.uid}"))
            user = self._items.get((household_id, user_id))
            if user is not None:
                user["displayName"] = display_name
                user["updatedAt"] = self._timestamp(datetime.now(UTC))
            self._identity_documents[identity.uid]["displayName"] = display_name

    def _update_identity_document(self, identity: AuthenticatedIdentity) -> None:
        document = self._identity_documents[identity.uid]
        if identity.email:
            document["email"] = identity.email
        if identity.display_name:
            document["displayName"] = identity.display_name
        household_id = self._identities[identity.uid]
        user_id = str(uuid5(NAMESPACE_URL, f"anke-user:{identity.uid}"))
        user = self._items.get((household_id, user_id))
        if user is not None:
            if identity.email:
                user["email"] = identity.email
            if identity.display_name:
                user["displayName"] = identity.display_name

    def household_for_uid(self, uid: str) -> str | None:
        return self._identities.get(uid)

    def delete_account_data(self, uid: str) -> int:
        with self._lock:
            household_id = self._identities.get(uid)
            if household_id is None:
                return 0
            keys = [key for key in self._items if key[0] == household_id]
            for key in keys:
                del self._items[key]
            self._identities.pop(uid, None)
            self._changes.pop(household_id, None)
            for key in [key for key in self._device_sequences if key[0] == household_id]:
                del self._device_sequences[key]
            self._identity_documents.pop(uid, None)
            return len(keys)

    def run_retention(self, now: datetime) -> RetentionResult:
        tombstone_cutoff = now - timedelta(days=30)
        audit_cutoff = now - timedelta(days=365)
        tombstone_payloads_purged = 0
        audit_events_deleted = 0
        with self._lock:
            for key, document in list(self._items.items()):
                if document.get("entityType") == "auditEvent":
                    created_at = self._parse_datetime(document.get("createdAt"))
                    if created_at is not None and created_at < audit_cutoff:
                        del self._items[key]
                        audit_events_deleted += 1
                    continue
                deleted_at = self._parse_datetime(document.get("deletedAt"))
                if (
                    deleted_at is None
                    or deleted_at >= tombstone_cutoff
                    or document.get("payloadPurgedAt") is not None
                ):
                    continue
                document["payload"] = None
                document["payloadPurgedAt"] = now.isoformat().replace("+00:00", "Z")
                tombstone_payloads_purged += 1
        return RetentionResult(tombstone_payloads_purged, audit_events_deleted)

    def replace_agent_api_key(
        self,
        household_id: str,
        actor: Actor,
        connection_id: str,
        key_hash: str,
        key_prefix: str,
        now: datetime,
    ) -> AgentAPIKeyView:
        timestamp = self._timestamp(now)
        document = {
            "id": connection_id,
            "entityType": "agentAPIKey",
            "householdId": household_id,
            "scopes": [scope.value for scope in AgentScope],
            "integration": OperationSource.skill.value,
            "status": "active",
            "keyHash": key_hash,
            "keyPrefix": key_prefix,
            "createdAt": timestamp,
            "updatedAt": timestamp,
        }
        with self._lock:
            previous = self._items.get((household_id, connection_id))
            legacy_keys = [
                key for key, value in self._items.items()
                if key[0] == household_id and value.get("entityType") == "agentConnection"
            ]
            for key in legacy_keys:
                del self._items[key]
            self._items[(household_id, connection_id)] = document
            action = "agent.api_key.reset" if previous is not None else "agent.api_key.create"
            self._record_authorization_audit(
                household_id, actor, document, action, "accepted"
            )
        return self._api_key_view(document)

    def agent_api_key(self, household_id: str) -> AgentAPIKeyView | None:
        documents = [
            item for (partition, _), item in self._items.items()
            if partition == household_id
            and item.get("entityType") == "agentAPIKey"
            and item.get("status") == "active"
        ]
        if not documents:
            return None
        return self._api_key_view(max(documents, key=lambda value: value["updatedAt"]))

    def revoke_agent_api_key(
        self,
        household_id: str,
        actor: Actor,
        now: datetime,
    ) -> AgentAPIKeyView | None:
        with self._lock:
            active = self.agent_api_key(household_id)
            if active is None:
                return None
            document = self._items[(household_id, str(active.connection_id))]
            document["status"] = "revoked"
            document["updatedAt"] = self._timestamp(now)
            self._record_authorization_audit(
                household_id, actor, document, "agent.api_key.revoke", "accepted"
            )
            return self._api_key_view(document)

    def consume_agent_request(
        self,
        household_id: str,
        connection_id: str,
        now: datetime,
        limit: int,
        window_seconds: int,
    ) -> bool:
        with self._lock:
            document = self._required_agent_api_key(household_id, connection_id)
            window_start = self._window_start(
                document.get("requestWindowStartedAt"), now, window_seconds
            )
            window_text = self._timestamp(window_start)
            count = (
                int(document.get("requestWindowCount", 0))
                if document.get("requestWindowStartedAt") == window_text
                else 0
            ) + 1
            document["requestWindowStartedAt"] = window_text
            document["requestWindowCount"] = count
            document["updatedAt"] = self._timestamp(now)
            if count > limit:
                if document.get("rateLimitAuditWindow") != window_text:
                    document["rateLimitAuditWindow"] = window_text
                    self._record_security_audit(
                        household_id,
                        document,
                        action="agent.rate_limit",
                        reason="requestRateExceeded",
                        marker=window_text,
                        count=count,
                    )
                return False
            document["lastUsedAt"] = self._timestamp(now)
            return True

    def authenticate_agent_api_key(
        self,
        household_id: str,
        connection_id: str,
        key_hash: str,
        now: datetime,
    ) -> AgentPrincipal | None:
        document = self._items.get((household_id, connection_id))
        if document is None or document.get("entityType") != "agentAPIKey":
            return None
        if document.get("status") != "active" or document.get("keyHash") != key_hash:
            return None
        return AgentPrincipal(
            household_id=UUID(household_id),
            connection_id=UUID(connection_id),
            scopes=[AgentScope(value) for value in document["scopes"]],
            integration=OperationSource(document.get("integration", "api")),
        )

    def record_agent_auth_failure(
        self,
        household_id: str,
        connection_id: str,
        reason: str,
        now: datetime,
        threshold: int,
        window_seconds: int,
    ) -> None:
        with self._lock:
            document = self._items.get((household_id, connection_id))
            if document is None or document.get("entityType") != "agentAPIKey":
                return
            window_start = self._window_start(
                document.get("failedAuthWindowStartedAt"), now, window_seconds
            )
            window_text = self._timestamp(window_start)
            count = (
                int(document.get("failedAuthWindowCount", 0))
                if document.get("failedAuthWindowStartedAt") == window_text
                else 0
            ) + 1
            document["failedAuthWindowStartedAt"] = window_text
            document["failedAuthWindowCount"] = count
            document["updatedAt"] = self._timestamp(now)
            operation_id = f"agent.auth:{connection_id}:{self._timestamp(now)}"
            self._items[(household_id, f"audit:{hashlib.sha256(operation_id.encode()).hexdigest()}")] = {
                "id": f"audit:{hashlib.sha256(operation_id.encode()).hexdigest()}",
                "entityType": "auditEvent",
                "householdId": household_id,
                "actor": {"type": "agent", "id": connection_id},
                "scope": "authentication",
                "action": "agent.authenticate",
                "source": document.get("integration", "api"),
                "targetId": connection_id,
                "operationId": operation_id,
                "outcome": "rejected",
                "reason": reason,
                "changeSummary": {},
                "createdAt": self._timestamp(now),
            }
            if (
                count >= threshold
                and document.get("failedAuthAnomalyWindow") != window_text
            ):
                document["failedAuthAnomalyWindow"] = window_text
                self._record_security_audit(
                    household_id,
                    document,
                    action="agent.authentication.anomaly",
                    reason="repeatedInvalidToken",
                    marker=window_text,
                    count=count,
                )

    def list_agent_entities(
        self,
        household_id: str,
        entity_types: set[str],
        limit: int,
    ) -> list[dict]:
        documents = [
            dict(item)
            for (partition, _), item in self._items.items()
            if partition == household_id
            and item.get("entityType") in entity_types
            and item.get("deletedAt") is None
            and item.get("staged") is not True
        ]
        return sorted(
            documents,
            key=lambda item: (item.get("updatedAt", ""), item["id"]),
            reverse=True,
        )[:limit]

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
    ) -> tuple[list[dict], str | None, bool]:
        try:
            offset = int(cursor or "0")
        except ValueError as exc:
            raise ValueError("Invalid Agent page cursor") from exc
        if offset < 0:
            raise ValueError("Invalid Agent page cursor")
        start_text = start_date.isoformat() if start_date is not None else None
        end_text = end_date.isoformat() if end_date is not None else None
        documents = []
        for (partition, _), item in self._items.items():
            if (
                partition != household_id
                or item.get("entityType") not in entity_types
                or item.get("deletedAt") is not None
                or item.get("staged") is True
            ):
                continue
            if item.get("entityType") not in always_include_entity_types:
                payload = item.get("payload") or item
                temporal_value = payload.get(temporal_field)
                if not isinstance(temporal_value, str):
                    continue
                date_text = temporal_value[:10]
                if start_text is not None and date_text < start_text:
                    continue
                if end_text is not None and date_text > end_text:
                    continue
            documents.append(dict(item))
        documents.sort(
            key=lambda item: (item.get("updatedAt", ""), item["id"]),
            reverse=True,
        )
        page = documents[offset:offset + limit]
        next_offset = offset + len(page)
        has_more = next_offset < len(documents)
        return page, str(next_offset) if has_more else None, has_more

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
    ) -> tuple[dict, bool]:
        request_hash = canonical_write_hash(
            actor=actor,
            scope=scope,
            action=action,
            source=source,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload,
        )
        operation_key = (household_id, f"operation:{idempotency_key}")
        with self._lock:
            existing_operation = self._items.get(operation_key)
            if existing_operation is not None:
                if existing_operation.get("requestHash") != request_hash:
                    raise ValueError(
                        "Idempotency key was already used for a different write"
                    )
                result = self._items.get(
                    (household_id, existing_operation["resultEntityId"])
                )
                if result is None:
                    raise RuntimeError("Stored operation result is missing")
                return dict(result), True
            if (household_id, entity_id) in self._items:
                raise ValueError("Entity already exists")
            timestamp = now.isoformat().replace("+00:00", "Z")
            document = {
                "id": entity_id,
                "entityType": entity_type,
                "householdId": household_id,
                "schemaVersion": 1,
                "revision": 1,
                "createdAt": timestamp,
                "updatedAt": timestamp,
                "deletedAt": None,
                "actor": actor.model_dump(mode="json"),
                "operationId": idempotency_key,
                "lastAcceptedMutationId": idempotency_key,
                "payload": payload,
            }
            operation = {
                "id": operation_key[1],
                "entityType": "operation",
                "householdId": household_id,
                "actor": actor.model_dump(mode="json"),
                "scope": scope,
                "action": action,
                "source": source,
                "idempotencyKey": idempotency_key,
                "requestHash": request_hash,
                "status": "accepted",
                "resultEntityId": entity_id,
                "changeSummary": change_summary,
                "createdAt": timestamp,
                "updatedAt": timestamp,
            }
            audit = {
                "id": f"audit:{idempotency_key}",
                "entityType": "auditEvent",
                "householdId": household_id,
                "actor": actor.model_dump(mode="json"),
                "scope": scope,
                "action": action,
                "source": source,
                "targetId": entity_id,
                "operationId": idempotency_key,
                "idempotencyKey": idempotency_key,
                "outcome": "accepted",
                "reason": None,
                "priorRevision": change_summary.get("before", {}).get("revision") if change_summary.get("before") else None,
                "newRevision": change_summary.get("after", {}).get("revision") if change_summary.get("after") else None,
                "changeSummary": change_summary,
                "createdAt": timestamp,
            }
            if related_update is not None:
                related_id = related_update.get("id")
                current_related = self._items.get((household_id, related_id))
                if current_related is None:
                    raise ValueError("Related entity not found")
                if current_related.get("revision") != related_update.get("revision", 0) - 1:
                    raise ValueError("Related entity changed concurrently")
                self._items[(household_id, related_id)] = dict(related_update)
                self._changes.setdefault(household_id, []).append(dict(related_update))
            self._items[(household_id, entity_id)] = document
            self._items[operation_key] = operation
            self._items[(household_id, audit["id"])] = audit
            self._changes.setdefault(household_id, []).append(document)
            return dict(document), False

    def record_agent_read(
        self,
        household_id: str,
        actor: Actor,
        scope: str,
        action: str,
        result_count: int,
        now: datetime,
    ) -> None:
        timestamp = now.isoformat().replace("+00:00", "Z")
        operation_id = f"{action}:{actor.id}:{timestamp}"
        audit = {
            "id": f"audit:{hashlib.sha256(operation_id.encode()).hexdigest()}",
            "entityType": "auditEvent",
            "householdId": household_id,
            "actor": actor.model_dump(mode="json"),
            "scope": scope,
            "action": action,
            "targetId": household_id,
            "operationId": operation_id,
            "outcome": "accepted",
            "reason": None,
            "changeSummary": {"resultCount": result_count},
            "createdAt": timestamp,
        }
        with self._lock:
            self._items[(household_id, audit["id"])] = audit

    def record_agent_denial(
        self,
        household_id: str,
        actor: Actor,
        required_scope: str,
        action: str,
        now: datetime,
    ) -> None:
        timestamp = now.isoformat().replace("+00:00", "Z")
        operation_id = f"{action}:denied:{actor.id}:{timestamp}"
        audit = {
            "id": f"audit:{hashlib.sha256(operation_id.encode()).hexdigest()}",
            "entityType": "auditEvent",
            "householdId": household_id,
            "actor": actor.model_dump(mode="json"),
            "scope": required_scope,
            "action": action,
            "targetId": household_id,
            "operationId": operation_id,
            "outcome": "rejected",
            "reason": "insufficientScope",
            "changeSummary": {},
            "createdAt": timestamp,
        }
        with self._lock:
            self._items[(household_id, audit["id"])] = audit

    def push_mutation(
        self,
        household_id: str,
        actor: Actor,
        mutation: SyncMutation,
    ) -> MutationResult:
        with self._lock:
            mutation_id = str(mutation.mutation_id)
            entity_id = str(mutation.entity_id)
            operation_key = (household_id, f"operation:{mutation_id}")
            existing_operation = self._items.get(operation_key)
            if existing_operation is not None:
                existing_result = MutationResult.model_validate(existing_operation["result"])
                if existing_result.reason != "outboxSequenceGap":
                    return existing_result
                self._items.pop(operation_key, None)
                self._items.pop((household_id, f"audit:{mutation_id}"), None)

            device_id = str(mutation.device_id)
            device = self._items.get((household_id, device_id))
            if device is None or device.get("entityType") != "device":
                return self._rejected_mutation(household_id, actor, mutation, "deviceNotRegistered")
            sequence_key = (household_id, device_id)
            last_sequence = self._device_sequences.get(sequence_key, 0)
            if mutation.sequence != last_sequence + 1:
                return MutationResult(
                    mutation_id=mutation.mutation_id,
                    entity_id=mutation.entity_id,
                    status=MutationStatus.rejected,
                    reason="outboxSequenceGap",
                    expected_sequence=last_sequence + 1,
                )

            current = self._items.get((household_id, entity_id))
            if mutation.action is MutationAction.create:
                if current is not None:
                    same_payload = current.get("payload") == mutation.payload and current.get("entityType") == mutation.entity_type.value
                    if not same_payload:
                        return self._conflict_mutation(household_id, actor, mutation, current, "entityAlreadyExists")
                    result = MutationResult(
                        mutation_id=mutation.mutation_id,
                        entity_id=mutation.entity_id,
                        status=MutationStatus.accepted,
                        revision=int(current["revision"]),
                        server_entity=dict(current),
                    )
                    self._record_operation_and_audit(household_id, actor, mutation, result)
                    self._device_sequences[sequence_key] = mutation.sequence
                    return result
                revision = 1
            else:
                if current is None:
                    return self._conflict_mutation(household_id, actor, mutation, None, "entityNotFound")
                if current.get("deletedAt") is not None:
                    return self._conflict_mutation(household_id, actor, mutation, current, "entityDeleted")
                if current.get("revision") != mutation.base_revision:
                    return self._conflict_mutation(household_id, actor, mutation, current, "staleRevision")
                revision = int(current["revision"]) + 1

            now = datetime.now(UTC)
            created_at = current.get("createdAt") if current else now.isoformat().replace("+00:00", "Z")
            payload = dict(mutation.payload or (current or {}).get("payload") or {})
            deleted_at = now.isoformat().replace("+00:00", "Z") if mutation.action is MutationAction.delete else None
            document = {
                "id": entity_id,
                "entityType": mutation.entity_type.value,
                "householdId": household_id,
                "schemaVersion": 1,
                "revision": revision,
                "createdAt": created_at,
                "updatedAt": now.isoformat().replace("+00:00", "Z"),
                "deletedAt": deleted_at,
                "deletion": (
                    {"actor": actor.model_dump(mode="json"), "reason": "userRequested"}
                    if deleted_at else None
                ),
                "actor": actor.model_dump(mode="json"),
                "operationId": mutation_id,
                "lastAcceptedMutationId": mutation_id,
                "payload": payload,
            }
            result = MutationResult(
                mutation_id=mutation.mutation_id,
                entity_id=mutation.entity_id,
                status=MutationStatus.accepted,
                revision=revision,
                server_entity=document,
            )
            self._items[(household_id, entity_id)] = document
            self._record_operation_and_audit(household_id, actor, mutation, result)
            self._device_sequences[sequence_key] = mutation.sequence
            self._changes.setdefault(household_id, []).append(document)
            return result

    def pull_changes(
        self,
        household_id: str,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[SyncChange], str | None, bool]:
        start = int(cursor or "0")
        all_changes = self._changes.get(household_id, [])
        page = all_changes[start:start + limit]
        next_position = start + len(page)
        changes = [self._as_sync_change(document) for document in page]
        return changes, str(next_position), next_position < len(all_changes)

    def list_audit_events(
        self,
        household_id: str,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[AuditEventView], str | None, bool]:
        start = int(cursor or "0")
        documents = sorted(
            (
                document
                for (partition, _), document in self._items.items()
                if partition == household_id and document.get("entityType") == "auditEvent"
            ),
            key=lambda item: (item["createdAt"], item["id"]),
            reverse=True,
        )
        page = documents[start:start + limit]
        next_position = start + len(page)
        events = [
            AuditEventView(
                operation_id=item["operationId"],
                idempotency_key=item.get("idempotencyKey", item["operationId"]),
                actor_type=item["actor"]["type"],
                actor_id=item["actor"]["id"],
                scope=item.get("scope", "owner.sync"),
                action=item["action"],
                source=item.get("source", "api"),
                target_id=item["targetId"],
                outcome=item["outcome"],
                reason=item.get("reason"),
                prior_revision=item.get("priorRevision"),
                new_revision=item.get("newRevision"),
                change_summary=item.get("changeSummary", {}),
                created_at=item["createdAt"],
            )
            for item in page
        ]
        return events, str(next_position), next_position < len(documents)

    def stage_migration(
        self,
        household_id: str,
        actor: Actor,
        request: MigrationUploadRequest,
    ) -> MigrationResponse:
        with self._lock:
            session_id = str(request.manifest.session_id)
            key = (household_id, f"migration:{session_id}")
            existing = self._items.get(key)
            if existing is not None:
                if existing["contentDigest"] != request.manifest.content_digest:
                    raise ValueError("Migration session was already used with different content")
                return MigrationResponse(
                    session_id=request.manifest.session_id,
                    status=MigrationStatus(existing["status"]),
                    household_id=UUID(household_id),
                    verified_counts=existing["verifiedCounts"],
                    content_digest=existing["contentDigest"],
                    replayed=True,
                )
            household = self._items.get((household_id, household_id))
            if household is None or household.get("entityType") != "household":
                raise ValueError("Migration household is missing")
            if household.get("status") != "empty":
                raise ValueError("Migration requires an empty Agent Cloud household")
            user_entities = [item for (partition, _), item in self._items.items() if partition == household_id and item.get("entityType") in {item.value for item in SyncEntityType}]
            if user_entities:
                raise ValueError("Migration requires an empty Agent Cloud household")
            verified_counts = dict(Counter(item.entity_type.value for item in request.items))
            if verified_counts != request.manifest.record_counts:
                raise ValueError("Migration record counts do not match the manifest")
            digest = self._migration_digest(request)
            if digest != request.manifest.content_digest:
                raise ValueError("Migration content digest does not match the manifest")
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            for item in request.items:
                document = {
                    "id": str(item.entity_id),
                    "entityType": item.entity_type.value,
                    "householdId": household_id,
                    "schemaVersion": request.manifest.schema_version,
                    "revision": 1,
                    "createdAt": item.created_at.isoformat().replace("+00:00", "Z"),
                    "updatedAt": now,
                    "deletedAt": item.deleted_at.isoformat().replace("+00:00", "Z") if item.deleted_at else None,
                    "actor": actor.model_dump(mode="json"),
                    "operationId": session_id,
                    "lastAcceptedMutationId": session_id,
                    "payload": item.payload,
                    "migrationSessionId": session_id,
                    "staged": True,
                }
                self._items[(household_id, document["id"])] = document
            self._items[key] = {
                "id": key[1],
                "entityType": "migrationSession",
                "householdId": household_id,
                "status": "staged",
                "verifiedCounts": verified_counts,
                "contentDigest": digest,
                "sourceMode": request.manifest.source_mode.value,
                "createdAt": now,
                "updatedAt": now,
            }
            return MigrationResponse(
                session_id=request.manifest.session_id,
                status=MigrationStatus.staged,
                household_id=UUID(household_id),
                verified_counts=verified_counts,
                content_digest=digest,
            )

    def activate_migration(
        self,
        household_id: str,
        actor: Actor,
        session_id: str,
        content_digest: str,
    ) -> MigrationResponse:
        with self._lock:
            session = self._items.get((household_id, f"migration:{session_id}"))
            if session is None:
                raise ValueError("Migration session was not staged")
            if session["contentDigest"] != content_digest:
                raise ValueError("Migration activation digest mismatch")
            replayed = session["status"] == "active"
            session["status"] = "active"
            for (partition, _), document in self._items.items():
                if partition == household_id and document.get("migrationSessionId") == session_id:
                    document["staged"] = False
                    if not replayed:
                        self._changes.setdefault(household_id, []).append(dict(document))
            household = self._items[(household_id, household_id)]
            household["status"] = "active"
            return MigrationResponse(
                session_id=UUID(session_id),
                status=MigrationStatus.active,
                household_id=UUID(household_id),
                verified_counts=session["verifiedCounts"],
                content_digest=content_digest,
                replayed=replayed,
            )

    def _conflict_mutation(self, household_id, actor, mutation, current, reason):
        result = MutationResult(
            mutation_id=mutation.mutation_id,
            entity_id=mutation.entity_id,
            status=MutationStatus.conflict,
            revision=current.get("revision") if current else None,
            reason=reason,
            server_entity=dict(current) if current else None,
        )
        self._record_operation_and_audit(household_id, actor, mutation, result)
        self._device_sequences[(household_id, str(mutation.device_id))] = mutation.sequence
        return result

    def _rejected_mutation(self, household_id, actor, mutation, reason):
        result = MutationResult(
            mutation_id=mutation.mutation_id,
            entity_id=mutation.entity_id,
            status=MutationStatus.rejected,
            reason=reason,
        )
        self._record_operation_and_audit(household_id, actor, mutation, result)
        if reason != "deviceNotRegistered" and reason != "outboxSequenceGap":
            self._device_sequences[(household_id, str(mutation.device_id))] = mutation.sequence
        return result

    def _record_operation_and_audit(self, household_id, actor, mutation, result):
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        mutation_id = str(mutation.mutation_id)
        operation = {
            "id": f"operation:{mutation_id}",
            "entityType": "operation",
            "householdId": household_id,
            "result": result.model_dump(by_alias=True, mode="json"),
            "createdAt": now,
        }
        audit = {
            "id": f"audit:{mutation_id}",
            "entityType": "auditEvent",
            "householdId": household_id,
            "actor": actor.model_dump(mode="json"),
            "scope": "owner.sync",
            "action": f"{mutation.entity_type.value}.{mutation.action.value}",
            "targetId": str(mutation.entity_id),
            "operationId": mutation_id,
            "outcome": result.status.value,
            "reason": result.reason,
            "changeSummary": {
                "fields": sorted(
                    key for key in (mutation.payload or {}).keys()
                    if key not in {"note", "amountInFen"}
                )
            },
            "createdAt": now,
        }
        self._items[(household_id, operation["id"])] = operation
        self._items[(household_id, audit["id"])] = audit

    def _record_authorization_audit(self, household_id, actor, connection, action, outcome):
        operation_id = f"{action}:{connection['id']}:{connection['updatedAt']}"
        audit = {
            "id": f"audit:{hashlib.sha256(operation_id.encode()).hexdigest()}",
            "entityType": "auditEvent",
            "householdId": household_id,
            "actor": actor.model_dump(mode="json"),
            "scope": "authorization.manage",
            "action": action,
            "targetId": connection["id"],
            "operationId": operation_id,
            "outcome": outcome,
            "reason": None,
            "changeSummary": {
                "scopes": connection["scopes"],
                "integration": connection.get("integration", "api"),
                "status": connection["status"],
            },
            "createdAt": connection["updatedAt"],
        }
        self._items[(household_id, audit["id"])] = audit

    @staticmethod
    def _api_key_view(document: dict) -> AgentAPIKeyView:
        return AgentAPIKeyView(
            connection_id=document["id"],
            key_prefix=document["keyPrefix"],
            scopes=document["scopes"],
            status=document["status"],
            created_at=document["createdAt"],
            last_used_at=document.get("lastUsedAt"),
        )

    def _required_agent_api_key(self, household_id: str, connection_id: str) -> dict:
        document = self._items.get((household_id, connection_id))
        if document is None or document.get("entityType") != "agentAPIKey":
            raise ValueError("Agent API key not found")
        return document

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _window_start(value: str | None, now: datetime, seconds: int) -> datetime:
        if value is not None:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if now < parsed + timedelta(seconds=seconds):
                return parsed
        return now

    def _record_security_audit(
        self,
        household_id: str,
        connection: dict,
        *,
        action: str,
        reason: str,
        marker: str,
        count: int,
    ) -> None:
        operation_id = f"{action}:{connection['id']}:{marker}"
        audit_id = f"audit:{hashlib.sha256(operation_id.encode()).hexdigest()}"
        self._items[(household_id, audit_id)] = {
            "id": audit_id,
            "entityType": "auditEvent",
            "householdId": household_id,
            "actor": {"type": "agent", "id": connection["id"]},
            "scope": "authentication",
            "action": action,
            "source": connection.get("integration", "api"),
            "targetId": connection["id"],
            "operationId": operation_id,
            "outcome": "rejected",
            "reason": reason,
            "changeSummary": {"attemptCount": count},
            "createdAt": connection["updatedAt"],
        }

    @staticmethod
    def _as_sync_change(document: dict) -> SyncChange:
        payload = document.get("payload")
        if payload is None and document.get("entityType") == "ledgerEntry":
            payload = {
                "kind": document["kind"],
                "direction": document["direction"],
                "occurredAt": document["occurredAt"],
                "monthStart": document["monthStart"],
                "channelId": document.get("channelId"),
                "categoryId": document["categoryId"],
                "amountInFen": document["amountInFen"],
                "note": document.get("note"),
            }
        return SyncChange(
            entity_type=document["entityType"],
            entity_id=document["id"],
            revision=document["revision"],
            updated_at=document["updatedAt"],
            deleted_at=document.get("deletedAt"),
            payload=payload,
        )

    @staticmethod
    def _migration_digest(request: MigrationUploadRequest) -> str:
        canonical = [
            item.model_dump(by_alias=True, mode="json", exclude_none=True)
            for item in sorted(request.items, key=lambda value: (value.entity_type.value, str(value.entity_id)))
        ]
        encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _parse_datetime(value) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    def create_ledger_entry(
        self,
        request: LedgerEntryCreate,
        actor: Actor,
    ) -> LedgerCreateResult:
        household_id = str(request.household_id)
        operation_item_id = f"operation:{request.idempotency_key}"
        request_hash = canonical_write_hash(
            actor=actor,
            scope="ledger:create",
            action="ledger.create",
            source=request.source,
            entity_type="ledgerEntry",
            entity_id=str(request.id),
            payload=request.model_dump(by_alias=True, mode="json"),
        )
        with self._lock:
            existing_operation = self._items.get((household_id, operation_item_id))
            if existing_operation is not None:
                if existing_operation.get("requestHash") != request_hash:
                    raise ValueError(
                        "Idempotency key was already used for a different write"
                    )
                entry_data = self._items[
                    (household_id, existing_operation["resultEntityId"])
                ]
                return LedgerCreateResult(
                    entry=LedgerEntryDocument.model_validate(entry_data),
                    replayed=True,
                )

            operation, entry, audit = build_ledger_transaction_documents(
                request,
                actor,
                datetime.now(UTC),
            )
            documents = [
                operation.as_cosmos_document(),
                entry.as_cosmos_document(),
                audit.as_cosmos_document(),
            ]
            for document in documents:
                key = (household_id, document["id"])
                if key in self._items:
                    raise ValueError(f"Document already exists: {document['id']}")
            for document in documents:
                self._items[(household_id, document["id"])] = document
            self._changes.setdefault(household_id, []).append(
                entry.as_cosmos_document()
            )
            return LedgerCreateResult(entry=entry, replayed=False)

    def read_household_document(
        self,
        household_id: str,
        item_id: str,
    ) -> dict | None:
        item = self._items.get((household_id, item_id))
        return dict(item) if item is not None else None

    def documents_for_household(self, household_id: str) -> list[dict]:
        return [
            dict(document)
            for (partition, _), document in self._items.items()
            if partition == household_id
        ]
