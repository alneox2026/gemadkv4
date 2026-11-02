import asyncio
from datetime import datetime, timezone

import pytest

from common.billing import customer_wallet_document_id
from services.agent_gateway_v3.app.core.config import get_settings
from services.agent_gateway_v3.app.core.errors import ApiError
from services.agent_gateway_v3.app.services.wallet_reservations import WalletBalanceGate


@pytest.fixture(autouse=True)
def _reset_gateway_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeSnapshot:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class FakeDocumentReference:
    def __init__(self, client, collection, document_id):
        self.client = client
        self.collection = collection
        self.document_id = document_id

    @property
    def key(self):
        return self.collection, self.document_id

    def get(self):
        return FakeSnapshot(self.client.documents.get(self.key))

    def update(self, updates):
        self.client.documents[self.key].update(updates)


class FakeCollectionReference:
    def __init__(self, client, collection):
        self.client = client
        self.collection_name = collection

    def document(self, document_id):
        return FakeDocumentReference(self.client, self.collection_name, document_id)


class FakeTransaction:
    def __init__(self, client):
        self.client = client

    def get(self, document_ref):
        return FakeSnapshot(self.client.documents.get(document_ref.key))

    def create(self, document_ref, data):
        if document_ref.key in self.client.documents:
            raise RuntimeError("duplicate create")
        self.client.documents[document_ref.key] = dict(data)

    def update(self, document_ref, updates):
        self.client.documents[document_ref.key].update(updates)


class FakeClient:
    def __init__(self):
        self.documents = {}

    def collection(self, collection):
        return FakeCollectionReference(self, collection)

    def transaction(self):
        return FakeTransaction(self)


def _enable_billing(monkeypatch):
    monkeypatch.setenv("BILLING_ENFORCEMENT_ENABLED", "true")
    monkeypatch.setenv("BILLING_MINIMUM_BALANCE_NANOS", "50000000")
    get_settings.cache_clear()


def _wallet(user_id, available_credit_nanos=50_000_000):
    return {
        "schema_version": 1,
        "billing_subject_id": user_id,
        "owner_uid": user_id,
        "currency": "USD",
        "status": "active",
        "available_credit_nanos": available_credit_nanos,
        "reserved_credit_nanos": 0,
        "settled_usage_nanos": 0,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }


def _service_with_wallet(user_id, wallet, client=None):
    settings = get_settings()
    client = client or FakeClient()
    client.documents[(settings.wallets_collection, customer_wallet_document_id(user_id))] = wallet
    return client, WalletBalanceGate(firestore_client_factory=lambda: client)


def _authorize(service, *, user_id="user-1", turn_id="turn-1", minimum=None):
    return asyncio.run(
        service.authorize(
            user_id=user_id,
            agent_id="maxima",
            request_id="req-1",
            turn_id=turn_id,
            minimum_balance_nanos=minimum,
        )
    )


def test_minimum_balance_gate_is_read_only_and_emits_zero_value_metadata(monkeypatch):
    _enable_billing(monkeypatch)
    user_id = "user-1"
    client, service = _service_with_wallet(user_id, _wallet(user_id))
    before = {key: dict(value) for key, value in client.documents.items()}

    context = _authorize(service)

    assert context.minimum_balance_nanos == 50_000_000
    assert context.event_metadata() == {
        "reservation_id": "turn-1",
        "billing_subject_id": user_id,
        "request_id": "req-1",
        "agent_id": "maxima",
        "reserved_amount_nanos": 0,
        "minimum_balance_nanos": 50_000_000,
        "billing_currency": "USD",
    }
    assert client.documents == before
    assert (get_settings().billing_reservations_collection, "turn-1") not in client.documents


def test_balance_below_gate_fails_before_any_wallet_write(monkeypatch):
    _enable_billing(monkeypatch)
    user_id = "user-1"
    client, service = _service_with_wallet(user_id, _wallet(user_id, 49_999_999))
    before = {key: dict(value) for key, value in client.documents.items()}

    with pytest.raises(ApiError) as exc_info:
        _authorize(service)

    assert exc_info.value.status_code == 402
    assert exc_info.value.code == "insufficient_credit"
    assert client.documents == before


def test_pending_usage_and_existing_debt_do_not_block_if_minimum_is_available(monkeypatch):
    _enable_billing(monkeypatch)
    user_id = "user-1"
    wallet = _wallet(user_id, 75_000_000)
    wallet["pending_usage_reconciliation_count"] = 12
    wallet["usage_debt_nanos"] = 900_000_000
    client, service = _service_with_wallet(user_id, wallet)
    before = {key: dict(value) for key, value in client.documents.items()}

    context = _authorize(service, turn_id="turn-next")

    assert context is not None
    assert client.documents == before


def test_custom_minimum_balance_is_supported(monkeypatch):
    _enable_billing(monkeypatch)
    user_id = "user-1"
    _client, service = _service_with_wallet(user_id, _wallet(user_id, 80_000_000))

    context = _authorize(service, minimum=75_000_000)

    assert context.minimum_balance_nanos == 75_000_000


@pytest.mark.parametrize(
    ("wallet", "status_code", "code"),
    [
        (None, 402, "wallet_not_ready"),
        ({"owner_uid": "someone-else", "billing_subject_id": "user-1", "status": "active", "available_credit_nanos": 50_000_000}, 403, "wallet_ownership_mismatch"),
        ({"owner_uid": "user-1", "billing_subject_id": "user-1", "status": "disabled", "available_credit_nanos": 50_000_000}, 403, "wallet_inactive"),
    ],
)
def test_missing_mismatched_or_inactive_wallet_is_rejected(monkeypatch, wallet, status_code, code):
    _enable_billing(monkeypatch)
    user_id = "user-1"
    client = FakeClient()
    if wallet is not None:
        client.documents[(get_settings().wallets_collection, customer_wallet_document_id(user_id))] = wallet
    service = WalletBalanceGate(firestore_client_factory=lambda: client)

    with pytest.raises(ApiError) as exc_info:
        _authorize(service)

    assert exc_info.value.status_code == status_code
    assert exc_info.value.code == code
