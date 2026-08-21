from datetime import UTC, date, datetime
import unittest
from uuid import uuid4

from pydantic import ValidationError

from app.models import (
    Actor,
    ActorType,
    AgentAssetBatchCreate,
    AgentAssetCreate,
    AgentLedgerBatchCreate,
    AgentLedgerEntryCreate,
    EntryKind,
    LedgerDirection,
    LedgerEntryCreate,
    build_ledger_transaction_documents,
)


def make_request(**overrides) -> LedgerEntryCreate:
    values = {
        "id": uuid4(),
        "idempotency_key": uuid4(),
        "source": "api",
        "household_id": uuid4(),
        "kind": EntryKind.monthly_summary,
        "direction": LedgerDirection.expense,
        "occurred_at": datetime(2026, 8, 1, tzinfo=UTC),
        "month_start": date(2026, 8, 1),
        "channel_id": "channel:alipay",
        "category_id": "category:dining",
        "amount_in_fen": 12345,
        "note": "synthetic test",
    }
    values.update(overrides)
    return LedgerEntryCreate(**values)


class LedgerDocumentModelTest(unittest.TestCase):
    def test_builds_operation_entry_and_audit_in_one_partition(self):
        request = make_request()
        actor = Actor(type=ActorType.user, id="apple:subject-1")
        operation, entry, audit = build_ledger_transaction_documents(
            request,
            actor,
            datetime(2026, 8, 4, tzinfo=UTC),
        )

        documents = [
            operation.as_cosmos_document(),
            entry.as_cosmos_document(),
            audit.as_cosmos_document(),
        ]
        self.assertEqual({item["householdId"] for item in documents}, {str(request.household_id)})
        self.assertEqual(operation.id, f"operation:{request.idempotency_key}")
        self.assertEqual(audit.id, f"audit:{request.idempotency_key}")
        self.assertEqual(audit.scope, "ledger:create")
        self.assertEqual(audit.source, "api")
        self.assertTrue(
            all(
                item["lastAcceptedMutationId"] == str(request.idempotency_key)
                for item in documents
            )
        )
        self.assertEqual(entry.amount_in_fen, 12345)
        self.assertEqual(audit.change_summary["after"]["amountInFen"], 12345)
        self.assertNotIn("note", audit.change_summary)
        self.assertNotIn("note", str(audit.change_summary))

    def test_income_rejects_payment_channel(self):
        with self.assertRaises(ValidationError):
            make_request(
                direction=LedgerDirection.income,
                channel_id="channel:bank",
            )

    def test_expense_requires_payment_channel(self):
        with self.assertRaises(ValidationError):
            make_request(channel_id=None)

    def test_rejects_floating_point_money(self):
        for value in (123.0, 123.45):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    make_request(amount_in_fen=value)

    def test_rejects_naive_timestamp_and_non_first_month_date(self):
        with self.assertRaises(ValidationError):
            make_request(occurred_at=datetime(2026, 8, 1))
        with self.assertRaises(ValidationError):
            make_request(month_start=date(2026, 8, 2))

    def test_batch_rejects_duplicate_ids_keys_and_more_than_25_entries(self):
        entry = AgentLedgerEntryCreate(
            id=uuid4(),
            idempotency_key=uuid4(),
            kind="transaction",
            direction="expense",
            occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
            month_start=date(2026, 8, 1),
            channel_id="channel:cash",
            category_id="category:dining",
            amount_in_fen=100,
        )
        with self.assertRaises(ValidationError):
            AgentLedgerBatchCreate(entries=[entry, entry])
        with self.assertRaises(ValidationError):
            AgentLedgerBatchCreate(
                entries=[
                    entry.model_copy(
                        update={"id": uuid4(), "idempotency_key": uuid4()}
                    )
                    for _ in range(26)
                ]
            )

    def test_asset_create_requires_consistent_classification_and_unique_batch_ids(self):
        request = AgentAssetCreate(
            account_id=uuid4(),
            snapshot_id=uuid4(),
            idempotency_key=uuid4(),
            name="  Brokerage  ",
            kind="asset",
            asset_group="financial",
            category_id="asset-category:stocks",
            money_bucket="risk",
            amount_in_fen=120_000,
            observed_at=datetime(2026, 8, 21, tzinfo=UTC),
        )
        self.assertEqual(request.name, "Brokerage")
        with self.assertRaises(ValidationError):
            request.model_copy(update={"money_bucket": None}).model_validate(
                request.model_copy(update={"money_bucket": None}).model_dump()
            )
        with self.assertRaises(ValidationError):
            AgentAssetCreate(
                **request.model_dump(exclude={"asset_group", "money_bucket"}),
                asset_group="living",
                money_bucket="risk",
            )
        with self.assertRaises(ValidationError):
            AgentAssetBatchCreate(accounts=[request, request])
        with self.assertRaises(ValidationError):
            AgentAssetCreate(
                **request.model_dump(exclude={"snapshot_id"}),
                snapshot_id=request.account_id,
            )


if __name__ == "__main__":
    unittest.main()
