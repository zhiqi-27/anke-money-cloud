from __future__ import annotations

from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from app.models import (
    AgentAssetBatchCreate,
    AgentAssetCreate,
    AgentAssetUpdate,
    AgentLedgerBatchCreate,
    AgentLedgerEntryCreate,
    AgentPrincipal,
    OperationSource,
)
from app.services import (
    AgentAccessService,
    AgentRateLimitExceededError,
    CloudService,
    InvalidAgentTokenError,
    ProEntitlementRequiredError,
)


class AgentMCPTokenVerifier:
    """Adapt the revocable Anke API Key to MCP bearer authentication."""

    async def verify_token(self, token: str) -> AccessToken | None:
        from app.dependencies import get_household_storage

        storage = get_household_storage()
        try:
            principal = AgentAccessService(
                storage,
                entitlement_checker=storage.has_active_pro_entitlement,
            ).authenticate(token)
        except (
            InvalidAgentTokenError,
            AgentRateLimitExceededError,
            ProEntitlementRequiredError,
        ):
            return None
        if principal.integration not in {OperationSource.mcp, OperationSource.skill}:
            return None
        connection_id = str(principal.connection_id)
        return AccessToken(
            token="<redacted>",
            client_id=connection_id,
            subject=connection_id,
            scopes=[scope.value for scope in principal.scopes],
            claims={
                "householdId": str(principal.household_id),
                "connectionId": connection_id,
                "integration": principal.integration.value,
            },
        )


def _principal() -> AgentPrincipal:
    access_token = get_access_token()
    claims = access_token.claims if access_token is not None else None
    if not isinstance(claims, dict):
        raise PermissionError("Authenticated Agent connection is required")
    return AgentPrincipal(
        household_id=UUID(str(claims["householdId"])),
        connection_id=UUID(str(claims["connectionId"])),
        scopes=list(access_token.scopes),
        integration=OperationSource(str(claims["integration"])),
    )


def _service() -> CloudService:
    from app.dependencies import get_household_storage, get_push_notification_service

    storage = get_household_storage()
    return CloudService(
        storage,
        change_notifier=get_push_notification_service().notify_household,
        entitlement_checker=storage.has_active_pro_entitlement,
    )


mcp_server = MCPServer(
    name="Anke Money",
    description="Nine non-destructive tools for one authorized Anke Money household.",
    instructions=(
        "Use only the granted capability. Never infer another household, modify "
        "authorization, delete permanently, send raw statements to Anke Money, or "
        "perform unconfirmed bulk asset changes. Obtain explicit user confirmation "
        "immediately before any ledger or asset write. Ledger and asset batches each "
        "require one complete batch summary and confirmation. Reuse each idempotency key only "
        "when retrying that exact write."
    ),
    token_verifier=AgentMCPTokenVerifier(),
    auth=AuthSettings(
        issuer_url="https://anke.money",
        resource_server_url=None,
        required_scopes=[],
    ),
)


@mcp_server.tool(
    name="ledger_read",
    description="Read ledger entries from the single household bound to this connection.",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
)
async def ledger_read(
    limit: Annotated[int, Field(ge=1, le=500)] = 200,
    cursor: Annotated[str | None, Field(max_length=16384)] = None,
    start_date: date | None = None,
    end_date: date | None = None,
    ctx: Context | None = None,
) -> dict:
    del ctx
    response = _service().agent_list_ledger_entries(
        _principal(), limit, cursor, start_date, end_date
    )
    return response.model_dump(by_alias=True, mode="json")


@mcp_server.tool(
    name="ledger_create",
    description=(
        "Append one ledger entry after explicit user confirmation. Supply a stable "
        "UUID idempotency_key and reuse it only when retrying the exact same write."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def ledger_create(
    id: UUID,
    idempotency_key: UUID,
    kind: str,
    direction: str,
    occurred_at: datetime,
    month_start: date,
    category_id: str,
    amount_in_fen: int,
    channel_id: str | None = None,
    note: str | None = None,
    ctx: Context | None = None,
) -> dict:
    del ctx
    request = AgentLedgerEntryCreate(
        id=id,
        idempotency_key=idempotency_key,
        kind=kind,
        direction=direction,
        occurred_at=occurred_at,
        month_start=month_start,
        channel_id=channel_id,
        category_id=category_id,
        amount_in_fen=amount_in_fen,
        note=note,
    )
    response = _service().agent_create_ledger_entry(_principal(), request)
    return response.model_dump(by_alias=True, mode="json")


@mcp_server.tool(
    name="ledger_create_batch",
    description=(
        "Append 1 through 25 ledger entries after showing the complete proposed "
        "batch and receiving one explicit user confirmation. Each entry needs its "
        "own stable UUID idempotency key; retry the unchanged batch safely."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def ledger_create_batch(
    entries: Annotated[
        list[AgentLedgerEntryCreate],
        Field(min_length=1, max_length=25),
    ],
    ctx: Context | None = None,
) -> dict:
    del ctx
    response = _service().agent_create_ledger_batch(
        _principal(), AgentLedgerBatchCreate(entries=entries)
    )
    return response.model_dump(by_alias=True, mode="json")


@mcp_server.tool(
    name="assets_read",
    description="Read asset accounts and dated snapshots for this connection's household.",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
)
async def assets_read(
    limit: Annotated[int, Field(ge=1, le=500)] = 200,
    cursor: Annotated[str | None, Field(max_length=16384)] = None,
    start_date: date | None = None,
    end_date: date | None = None,
    ctx: Context | None = None,
) -> dict:
    del ctx
    response = _service().agent_list_assets(
        _principal(), limit, cursor, start_date, end_date
    )
    return response.model_dump(by_alias=True, mode="json")


@mcp_server.tool(
    name="assets_create",
    description=(
        "Create one asset account and its initial dated snapshot atomically after "
        "explicit user confirmation. Resolve an active asset category first."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def assets_create(
    account_id: UUID,
    snapshot_id: UUID,
    idempotency_key: UUID,
    name: Annotated[str, Field(min_length=1, max_length=80)],
    kind: str,
    category_id: Annotated[str, Field(min_length=1, max_length=128)],
    amount_in_fen: int,
    observed_at: datetime,
    asset_group: str | None = None,
    money_bucket: str | None = None,
    member_profile_id: str | None = None,
    ctx: Context | None = None,
) -> dict:
    del ctx
    request = AgentAssetCreate(
        account_id=account_id,
        snapshot_id=snapshot_id,
        idempotency_key=idempotency_key,
        name=name,
        kind=kind,
        asset_group=asset_group,
        category_id=category_id,
        money_bucket=money_bucket,
        amount_in_fen=amount_in_fen,
        observed_at=observed_at,
        member_profile_id=member_profile_id,
    )
    response = _service().agent_create_asset(_principal(), request)
    return response.model_dump(by_alias=True, mode="json")


@mcp_server.tool(
    name="assets_create_batch",
    description=(
        "Create 1 through 25 asset accounts with initial snapshots after showing "
        "the complete proposed batch and receiving one explicit confirmation."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def assets_create_batch(
    accounts: Annotated[
        list[AgentAssetCreate],
        Field(min_length=1, max_length=25),
    ],
    ctx: Context | None = None,
) -> dict:
    del ctx
    response = _service().agent_create_asset_batch(
        _principal(), AgentAssetBatchCreate(accounts=accounts)
    )
    return response.model_dump(by_alias=True, mode="json")


@mcp_server.tool(
    name="assets_update",
    description=(
        "Update exactly one asset account after explicit user confirmation by appending "
        "one dated snapshot. Bulk updates and history replacement are not supported."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def assets_update(
    account_id: UUID,
    snapshot_id: UUID,
    idempotency_key: UUID,
    amount_in_fen: int,
    observed_at: datetime,
    member_profile_id: str | None = None,
    ctx: Context | None = None,
) -> dict:
    del ctx
    request = AgentAssetUpdate(
        snapshot_id=snapshot_id,
        idempotency_key=idempotency_key,
        member_profile_id=member_profile_id,
        amount_in_fen=amount_in_fen,
        observed_at=observed_at,
    )
    response = _service().agent_update_asset(_principal(), account_id, request)
    return response.model_dump(by_alias=True, mode="json")


@mcp_server.tool(
    name="categories_read",
    description="Read categories from the single household bound to this connection.",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
)
async def categories_read(
    limit: Annotated[int, Field(ge=1, le=500)] = 200,
    ctx: Context | None = None,
) -> dict:
    del ctx
    response = _service().agent_list_categories(_principal(), limit)
    return response.model_dump(by_alias=True, mode="json")


@mcp_server.tool(
    name="channels_read",
    description="Read payment channels from the single household bound to this connection.",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
)
async def channels_read(
    limit: Annotated[int, Field(ge=1, le=500)] = 200,
    ctx: Context | None = None,
) -> dict:
    del ctx
    response = _service().agent_list_channels(_principal(), limit)
    return response.model_dump(by_alias=True, mode="json")


def build_mcp_asgi_app():
    from app.config import get_settings

    settings = get_settings()
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(settings.mcp_allowed_hosts),
        allowed_origins=list(settings.mcp_allowed_origins),
    )
    return mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=security,
    )


mcp_asgi_app = build_mcp_asgi_app()
