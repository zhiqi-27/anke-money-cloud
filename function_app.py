import azure.functions as func
import logging
from datetime import UTC, datetime

from app.dependencies import get_household_storage
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
