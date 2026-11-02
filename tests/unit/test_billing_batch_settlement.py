from datetime import datetime, timezone

import pytest

from common.billing import customer_billing_period_document_id, customer_wallet_document_id
from services.agent_persistence_worker_v3.app.core.config import get_settings
from services.agent_persistence_worker_v3.app.services.billing_batch_settlement import (
    BillingBatchSettlementService,
)
from tests.unit.test_wallet_reservations import FakeClient, FakeSnapshot


@pytest.fixture(autouse=True)
def _reset_worker_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _run_transaction(client, operation):
    return operation(client.transaction())


class SnapshotWithReference(FakeSnapshot):
    def __init__(self, data, reference):
        super().__init__(data)
        self.reference = reference


def test_batch_settlement_charges_available_balance_records_debt_and_is_idempotent():
    settings = get_settings()
    client = FakeClient()
    subject_id = "user-batch"
    wallet_id = customer_wallet_document_id(subject_id)
    period_key = "2026-11"
    period_id = customer_billing_period_document_id(subject_id, period_key)
    created_at = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    client.documents[(settings.wallets_collection, wallet_id)] = {
        "billing_subject_id": subject_id,
        "owner_uid": subject_id,
        "status": "active",
        "available_credit_nanos": 100_000_000,
        "reserved_credit_nanos": 0,
        "pending_batch_usage_nanos": 160_000_000,
        "settled_usage_nanos": 0,
        "usage_debt_nanos": 0,
    }

    rows = []
    for turn_id, cost_nanos in (("turn-a", 80_000_000), ("turn-b", 80_000_000)):
        reservation = {
            "turn_id": turn_id,
            "owner_uid": subject_id,
            "billing_subject_id": subject_id,
            "status": "batch_pending",
            "estimated_cost_nanos": cost_nanos,
            "period_key": period_key,
            "period_start": datetime(2026, 11, 1, tzinfo=timezone.utc),
            "period_end": datetime(2026, 12, 1, tzinfo=timezone.utc),
            "billing_batch_created_at": created_at,
            "turn_created_at": created_at,
            "late_usage": False,
        }
        client.documents[(settings.billing_reservations_collection, turn_id)] = reservation
        client.documents[(settings.billing_ledger_collection, turn_id)] = {
            "turn_id": turn_id,
            "uid": subject_id,
            "billing_subject_id": subject_id,
            "estimated_cost_usd_nanos": cost_nanos,
        }
        reference = client.collection(settings.billing_reservations_collection).document(turn_id)
        rows.append((SnapshotWithReference(reservation, reference), reservation))

    service = BillingBatchSettlementService(
        firestore_client_factory=lambda: client,
        transaction_runner=lambda _client, operation: _run_transaction(client, operation),
    )

    result = service._settle_batch(client, rows)
    duplicate_result = service._settle_batch(client, rows)

    assert result is not None
    assert result.status == "settled_shortfall"
    assert result.turn_count == 2
    assert result.estimated_cost_nanos == 160_000_000
    assert result.charged_amount_nanos == 100_000_000
    assert result.shortfall_nanos == 60_000_000
    assert duplicate_result == result

    wallet = client.documents[(settings.wallets_collection, wallet_id)]
    assert wallet["available_credit_nanos"] == 0
    assert wallet["pending_batch_usage_nanos"] == 0
    assert wallet["settled_usage_nanos"] == 100_000_000
    assert wallet["usage_debt_nanos"] == 60_000_000
    transaction = client.documents[(settings.wallet_transactions_collection, result.batch_id)]
    assert transaction["amount_nanos"] == 100_000_000
    assert transaction["shortfall_nanos"] == 60_000_000
    assert transaction["usage_debt_added_nanos"] == 60_000_000
    assert (settings.customer_billing_periods_collection, period_id) in client.documents
