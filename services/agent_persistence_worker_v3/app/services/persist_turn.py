"""Turn persistence orchestration for Pub/Sub-delivered events."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from common.schemas import TurnCompletedEvent
from services.agent_persistence_worker_v3.app.core.errors import RetryableWorkerError
from services.agent_persistence_worker_v3.app.core.config import get_settings
from services.agent_persistence_worker_v3.app.services.firestore_client import (
    get_firestore_client,
)
from services.agent_persistence_worker_v3.app.services.billing_ledger import (
    BillingLedgerRepository,
)
from services.agent_persistence_worker_v3.app.services.billing_reservation_repository import (
    BillingReservationRepository,
    has_pending_provider_usage,
)
from services.agent_persistence_worker_v3.app.services.billing_batch_settlement import (
    BillingBatchSettlementService,
)
from services.agent_persistence_worker_v3.app.services.billing_settlement import (
    BillingSettlementService,
)
from services.agent_persistence_worker_v3.app.services.firestore_messages import (
    FirestoreMessagesRepository,
)
from services.agent_persistence_worker_v3.app.services.firestore_threads import (
    FirestoreThreadsRepository,
)
from services.agent_persistence_worker_v3.app.services.idempotency import IdempotencyStore


@dataclass(frozen=True)
class PersistTurnResult:
    event_id: str
    thread_id: str
    persisted: bool
    ignored_reason: str | None = None
    billing_settlement_status: str | None = None


class PersistTurnService:
    def __init__(
        self,
        idempotency_store: IdempotencyStore | None = None,
        billing_ledger_repository: BillingLedgerRepository | None = None,
        billing_reservation_repository: BillingReservationRepository | None = None,
        billing_settlement_service: BillingSettlementService | None = None,
        billing_batch_settlement_service: BillingBatchSettlementService | None = None,
        threads_repository: FirestoreThreadsRepository | None = None,
        messages_repository: FirestoreMessagesRepository | None = None,
        firestore_client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.idempotency_store = idempotency_store or IdempotencyStore()
        self.billing_ledger_repository = (
            billing_ledger_repository or BillingLedgerRepository()
        )
        self.billing_reservation_repository = (
            billing_reservation_repository or BillingReservationRepository()
        )
        self.billing_settlement_service = (
            billing_settlement_service or BillingSettlementService()
        )
        self.billing_batch_settlement_service = (
            billing_batch_settlement_service or BillingBatchSettlementService()
        )
        self.threads_repository = (
            threads_repository or FirestoreThreadsRepository()
        )
        self.messages_repository = (
            messages_repository or FirestoreMessagesRepository()
        )
        self.firestore_client_factory = firestore_client_factory or get_firestore_client

    async def persist(self, event: TurnCompletedEvent) -> PersistTurnResult:
        return await asyncio.to_thread(self._persist_sync, event)

    def _persist_sync(self, event: TurnCompletedEvent) -> PersistTurnResult:
        try:
            client = self.firestore_client_factory()
            existing_thread = self.threads_repository.load_existing(
                client,
                event.thread_id,
            )
            ignored_reason = self.threads_repository.validate_existing_for_turn(
                existing_thread,
                event,
            )
            if ignored_reason:
                return self._persist_idempotency_only(
                    client,
                    event,
                    ignored_reason=ignored_reason,
                )
            batch = client.batch()
            self.idempotency_store.add_create_to_batch(batch, client, event)
            self.billing_ledger_repository.add_create_to_batch(batch, client, event)
            self.billing_reservation_repository.add_create_to_batch(batch, client, event)
            self.threads_repository.add_upsert_to_batch(
                batch,
                client,
                event,
                existing_thread=existing_thread,
            )
            self.messages_repository.add_turn_messages_to_batch(
                batch,
                client,
                event,
            )
            batch.commit()
        except Exception as exc:
            if self._is_conflict_error(exc):
                persisted = False
            else:
                raise RetryableWorkerError(
                    f"Failed to persist turn event {event.event_id}: {exc}"
                ) from exc
        else:
            persisted = True

        settlement_status = None
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        billing = metadata.get("billing")
        usage = event.usage if isinstance(event.usage, dict) else {}
        if isinstance(billing, dict):
            has_price = _has_estimated_cost(usage)
            provider = metadata.get("provider")
            provider_metadata = provider if isinstance(provider, dict) else {}
            provider_usage_pending = (
                has_pending_provider_usage(
                    usage,
                    provider_name=provider_metadata.get("name"),
                )
                or
                usage.get("billing_status") == "pending_provider_usage"
                or usage.get("usage_reporting_complete") is False
                or not has_price
            )
            settings = get_settings()
            try:
                if settings.billing_batch_settlement_enabled:
                    if has_price and not provider_usage_pending:
                        queued = self.billing_batch_settlement_service.enqueue_turn_sync(
                            event
                        )
                        settlement_status = "batch_pending" if queued else "already_queued"
                    else:
                        settlement_status = "late_usage_pending"
                else:
                    settlement = self.billing_settlement_service.settle_if_required_sync(
                        event
                    )
                    settlement_status = settlement.status if settlement else None
            except Exception as exc:
                if isinstance(exc, RetryableWorkerError):
                    raise
                raise RetryableWorkerError(
                    f"Failed to schedule billing settlement for turn {event.turn_id}: {exc}"
                ) from exc

        return PersistTurnResult(
            event_id=event.event_id,
            thread_id=event.thread_id,
            persisted=persisted,
            billing_settlement_status=settlement_status,
        )

    def _persist_idempotency_only(
        self,
        client: Any,
        event: TurnCompletedEvent,
        *,
        ignored_reason: str,
    ) -> PersistTurnResult:
        batch = client.batch()
        self.idempotency_store.add_create_to_batch(batch, client, event)
        try:
            batch.commit()
        except Exception as exc:
            if self._is_conflict_error(exc):
                return PersistTurnResult(
                    event_id=event.event_id,
                    thread_id=event.thread_id,
                    persisted=False,
                    ignored_reason=ignored_reason,
                )
            raise RetryableWorkerError(
                f"Failed to record ignored turn event {event.event_id}: {exc}"
            ) from exc
        return PersistTurnResult(
            event_id=event.event_id,
            thread_id=event.thread_id,
            persisted=False,
            ignored_reason=ignored_reason,
        )

    def _is_conflict_error(self, exc: Exception) -> bool:
        if exc.__class__.__name__ == "Conflict":
            return True
        try:
            from google.cloud import exceptions as cloud_exceptions

            return isinstance(exc, cloud_exceptions.Conflict)
        except ModuleNotFoundError:
            return False


def _has_estimated_cost(usage: dict[str, Any]) -> bool:
    value = usage.get("estimated_cost_usd")
    if isinstance(value, bool):
        return False
    try:
        from decimal import Decimal, InvalidOperation

        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return False
    return amount.is_finite() and amount >= 0

