from datetime import UTC, date, datetime
import unittest
from uuid import uuid4

from pydantic import ValidationError

from app.models import (
    Actor,
    ActorType,
    EntryKind,
    LedgerDirection,
    LedgerEntryCreate,
    build_ledger_transaction_documents,
)


def make_request(**overrides) -> LedgerEntryCreate:
    values = {
        "id": uuid4(),
        "operation_id": uuid4(),
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
        actor = Actor(type=ActorType.user, id="firebase-user-1")
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
        self.assertEqual(operation.id, f"operation:{request.operation_id}")
        self.assertEqual(audit.id, f"audit:{request.operation_id}")
        self.assertEqual(audit.scope, "ledger.entry.create")
        self.assertTrue(
            all(
                item["lastAcceptedMutationId"] == str(request.operation_id)
                for item in documents
            )
        )
        self.assertEqual(entry.amount_in_fen, 12345)
        self.assertNotIn("amountInFen", audit.change_summary)
        self.assertNotIn("note", audit.change_summary)

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


if __name__ == "__main__":
    unittest.main()
