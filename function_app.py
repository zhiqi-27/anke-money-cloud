import azure.functions as func
import json
import logging
import os
from datetime import UTC, datetime

from app.dependencies import get_household_storage, get_push_notification_service
from app.main import fastapi_app


app = func.AsgiFunctionApp(
    app=fastapi_app,
    http_auth_level=func.AuthLevel.ANONYMOUS,
)


@app.timer_trigger(
    schedule="0 0 3 * * *",
    arg_name="retention_timer",
    run_on_startup=False,
    use_monitor=True,
)
def enforce_data_retention(retention_timer: func.TimerRequest) -> None:
    result = get_household_storage().run_retention(datetime.now(UTC))
    logging.getLogger(__name__).info(
        "Retention completed, tombstone_payloads_purged=%s audit_events_deleted=%s",
        result.tombstone_payloads_purged,
        result.audit_events_deleted,
    )


@app.cosmos_db_trigger(
    arg_name="documents",
    database_name=os.getenv("ANKE_COSMOS_DATABASE", "anke_money_dev"),
    container_name=os.getenv("ANKE_COSMOS_ENTITIES_CONTAINER", "anke_entities"),
    connection="ANKE_COSMOS_CHANGE_FEED",
    lease_container_name=os.getenv("ANKE_COSMOS_LEASES_CONTAINER", "anke_sync_leases"),
    create_lease_container_if_not_exists=False,
    max_items_per_invocation=100,
)
def notify_devices_of_cloud_changes(documents: func.DocumentList) -> None:
    synchronized_entity_types = {
        "ledgerEntry",
        "assetAccount",
        "assetSnapshot",
        "paymentChannel",
        "category",
        "memberProfile",
    }
    household_ids: set[str] = set()
    for document in documents:
        if hasattr(document, "to_json"):
            value = json.loads(document.to_json())
        else:
            value = dict(document)
        household_id = value.get("householdId")
        if (
            value.get("entityType") in synchronized_entity_types
            and isinstance(household_id, str)
            and household_id
        ):
            household_ids.add(household_id)

    notifier = get_push_notification_service()
    for household_id in sorted(household_ids):
        notifier.notify_household(household_id)
