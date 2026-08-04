from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.config import ConfigurationError, Settings  # noqa: E402
from app.storage.cosmos import CosmosHouseholdStorage  # noqa: E402


def validate_smoke_target(settings: Settings, storage: CosmosHouseholdStorage) -> None:
    if settings.environment != "dev":
        raise ConfigurationError("Smoke writes require ANKE_ENVIRONMENT=dev")
    if not settings.cosmos_allow_smoke_write:
        raise ConfigurationError(
            "Smoke writes require ANKE_COSMOS_ALLOW_SMOKE_WRITE=true"
        )
    if not settings.cosmos_expected_account_name:
        raise ConfigurationError("Expected Cosmos account name is required")
    if storage.account_name != settings.cosmos_expected_account_name.lower():
        raise ConfigurationError("Cosmos endpoint does not match expected Dev account")


def main() -> int:
    settings = Settings.from_environment()
    settings.require_cosmos()
    storage = CosmosHouseholdStorage(settings)
    validate_smoke_target(settings, storage)
    storage.verify_household_partition_contract()

    run_id = str(uuid4())
    household_id = f"smoke-dev-{run_id}"
    item_id = f"smoke:{run_id}"
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    probe = {
        "id": item_id,
        "entityType": "smokeProbe",
        "householdId": household_id,
        "schemaVersion": 1,
        "revision": 1,
        "createdAt": now,
        "updatedAt": now,
        "deletedAt": None,
        "actor": {"type": "system", "id": "development-smoke"},
        "operationId": run_id,
        "isSynthetic": True,
        "runId": run_id,
    }
    stored = storage.create_and_read_smoke_probe(probe)
    if stored.get("id") != item_id or stored.get("householdId") != household_id:
        raise RuntimeError("Cosmos smoke read-back did not match the created probe")

    print("Development Cosmos smoke passed")
    print(f"account={storage.account_name}")
    print(f"database={settings.cosmos_database}")
    print(f"container={settings.cosmos_entities_container}")
    print(f"run_id={run_id}")
    print(f"item_id={item_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
