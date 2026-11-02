"""Reconciliation for reservations whose completed-turn event never arrived."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from common.billing import customer_wallet_document_id, nonnegative_int
from common.firestore import get_transaction_document_snapshot
from common.schemas import TurnCompletedEvent
from services.agent_persistence_worker_v3.app.core.config import get_settings
from services.agent_persistence_worker_v3.app.core.errors import RetryableWorkerError
from services.agent_persistence_worker_v3.app.services.billing_settlement import (
    BillingSettlementService,
)
from services.agent_persistence_worker_v3.app.services.billing_batch_settlement import (
    BillingBatchSettlementService,
)
from services.agent_persistence_worker_v3.app.services.billing_ledger import (
    BillingLedgerRepository,
)
from services.agent_persistence_worker_v3.app.services.agent_runtime_session_usage_hydrator import (
    AgentRuntimeSessionUsageHydrator,
)
from services.agent_persistence_worker_v3.app.services.firestore_client import (
    get_firestore_client,
)
from services.agent_persistence_worker_v3.app.services.firestore_messages import (
    FirestoreMessagesRepository,
)

LOGGER = logging.getLogger(__name__)
MAX_USAGE_LOOKUPS_PER_PASS = 4


@dataclass(frozen=True)
class BillingReconciliationResult:
    scanned_reservations: int
    settled_reservations: int
    released_reservations: int
    skipped_reservations: int


class BillingReconciliationService:
    """Recover late Agent Runtime usage and reconcile V4 markers and legacy holds."""

    def __init__(
        self,
        *,
        firestore_client_factory: Callable[[], Any] | None = None,
        settlement_service: BillingSettlementService | None = None,
        usage_hydrator: AgentRuntimeSessionUsageHydrator | None = None,
        messages_repository: FirestoreMessagesRepository | None = None,
    ) -> None:
        self._settings = get_settings()
        self._firestore_client_factory = firestore_client_factory or get_firestore_client
        self._settlement_service = settlement_service or BillingSettlementService(
            firestore_client_factory=self._firestore_client_factory,
        )
        self._batch_settlement_service = BillingBatchSettlementService(
            firestore_client_factory=self._firestore_client_factory,
        )
        self._ledger_repository = BillingLedgerRepository(
            self._settings.billing_ledger_collection
        )
        self._usage_hydrator = usage_hydrator or AgentRuntimeSessionUsageHydrator()
        self._messages_repository = messages_repository or FirestoreMessagesRepository()

    async def reconcile_expired(self) -> BillingReconciliationResult:
        return await asyncio.to_thread(self.reconcile_expired_sync)

    def reconcile_expired_sync(self) -> BillingReconciliationResult:
        client = self._firestore_client_factory()
        now = datetime.now(timezone.utc)
        reservations = self._expired_reservations(client, now)
        scanned = settled = released = skipped = 0
        for reservation_snapshot in reservations:
            scanned += 1
            reservation = reservation_snapshot.to_dict() or {}
            if reservation.get("status") != "reserved":
                skipped += 1
                continue
            turn_id = _required_string(reservation.get("turn_id"), "turn_id")
            ledger_snapshot = client.collection(
                self._settings.billing_ledger_collection
            ).document(turn_id).get()
            if ledger_snapshot.exists:
                event = _event_from_ledger_and_reservation(
                    ledger_snapshot.to_dict() or {},
                    reservation,
                )
                result = self._settlement_service.settle_if_required_sync(event)
                if result is not None and result.status not in {
                    "usage_reconciliation_pending",
                    "usage_recovery_manual_review",
                }:
                    settled += 1
                else:
                    skipped += 1
                continue
            if self._hold_expired_reservation_for_review_sync(client, reservation, now):
                skipped += 1
            else:
                skipped += 1
        manual_review = list(self._manual_review_reservations(client))
        for reservation_snapshot in manual_review:
            scanned += 1
            reservation = reservation_snapshot.to_dict() or {}
            # V4 zero-value turn markers have no wallet hold to release or
            # legacy pending-usage counter to register. Their manual-review
            # state is terminal for automated recovery; keep this migration
            # path limited to historical reservation records.
            if reservation.get("authorization_mode") == "minimum_balance_check":
                skipped += 1
                continue
            missing_ledger_reasons = {
                "billing_ledger_missing",
                "completed_turn_ledger_missing_after_reservation_expiry",
            }
            if (
                reservation.get("usage_recovery_hold_released")
                and reservation.get("usage_reconciliation_manual_review_reason")
                not in missing_ledger_reasons
            ):
                continue
            turn_id = _required_string(reservation.get("turn_id"), "turn_id")
            ledger_snapshot = client.collection(
                self._settings.billing_ledger_collection
            ).document(turn_id).get()
            if not ledger_snapshot.exists:
                if not reservation.get("usage_recovery_hold_released"):
                    self._hold_expired_reservation_for_review_sync(
                        client, reservation, now
                    )
                else:
                    self._mark_usage_manual_review(
                        client, reservation, now, reason="billing_ledger_missing"
                    )
                skipped += 1
                continue
            event = _event_from_ledger_and_reservation(
                ledger_snapshot.to_dict() or {}, reservation
            )
            try:
                result = self._settlement_service.settle_if_required_sync(event)
            except RetryableWorkerError:
                skipped += 1
                continue
            if result is None or result.status in {
                "usage_reconciliation_pending",
                "usage_recovery_manual_review",
            }:
                skipped += 1
            else:
                settled += 1
        pending = list(self._pending_usage_reservations(client, now))
        for reservation_snapshot in pending[:MAX_USAGE_LOOKUPS_PER_PASS]:
            scanned += 1
            reservation = reservation_snapshot.to_dict() or {}
            turn_id = _required_string(reservation.get("turn_id"), "turn_id")
            ledger_ref = client.collection(
                self._settings.billing_ledger_collection
            ).document(turn_id)
            ledger_snapshot = ledger_ref.get()
            if not ledger_snapshot.exists:
                released = self._hold_expired_reservation_for_review_sync(
                    client, reservation, now
                )
                if not released:
                    self._mark_usage_manual_review(
                        client, reservation, now, reason="billing_ledger_missing"
                    )
                skipped += 1
                continue
            ledger = ledger_snapshot.to_dict() or {}
            # Migrate reservations created by older workers, which registered
            # unresolved usage but kept the wallet hold. Release that hold
            # before another provider lookup so an old pending turn cannot
            # prevent a new request from using available credit.
            if (
                _ledger_cost_nanos(ledger) is None
                and not reservation.get("usage_recovery_hold_released")
            ):
                event = _event_from_ledger_and_reservation(ledger, reservation)
                try:
                    migration_result = self._settlement_service.settle_if_required_sync(
                        event
                    )
                except RetryableWorkerError:
                    self._schedule_usage_recovery_retry(client, reservation, now)
                    skipped += 1
                    continue
                if migration_result is None:
                    self._schedule_usage_recovery_retry(client, reservation, now)
                    skipped += 1
                    continue
                if migration_result.status not in {
                    "usage_reconciliation_pending",
                    "usage_recovery_manual_review",
                }:
                    settled += 1
                    continue
                reservation["usage_recovery_hold_released"] = True
                if migration_result.status == "usage_recovery_manual_review":
                    skipped += 1
                    continue

            if _ledger_cost_nanos(ledger) is not None:
                event = _event_from_ledger_and_reservation(ledger, reservation)
                result = self._settlement_service.settle_if_required_sync(event)
                if result is not None and result.status not in {
                    "usage_reconciliation_pending",
                    "usage_recovery_manual_review",
                }:
                    settled += 1
                else:
                    skipped += 1
                continue

            # A previous pass may have retrieved usage but failed while
            # settling it. Reuse that persisted result before querying Google
            # again; the immutable ledger remains untouched.
            recovered_usage = reservation.get("recovered_usage")
            if isinstance(recovered_usage, dict) and _ledger_cost_nanos(
                recovered_usage
            ) is not None:
                event = _event_from_ledger_and_reservation(ledger, reservation)
                try:
                    result = self._settlement_service.settle_if_required_sync(event)
                except RetryableWorkerError:
                    self._schedule_usage_recovery_retry(client, reservation, now)
                    skipped += 1
                    continue
                if result is None or result.status in {
                    "usage_reconciliation_pending",
                    "usage_recovery_manual_review",
                }:
                    self._schedule_usage_recovery_retry(client, reservation, now)
                    skipped += 1
                else:
                    settled += 1
                continue

            deadline = _as_utc_or_none(
                reservation.get("usage_reconciliation_deadline_at")
            )
            if deadline is not None and now >= deadline:
                self._mark_usage_manual_review(
                    client,
                    reservation,
                    now,
                    reason="provider_usage_retention_expired",
                )
                skipped += 1
                continue
            next_attempt = _as_utc_or_none(
                reservation.get("usage_reconciliation_next_attempt_at")
            )
            if next_attempt is not None and now < next_attempt:
                skipped += 1
                continue

            provider = ledger.get("provider_usage_recovery")
            provider = provider if isinstance(provider, dict) else {}
            if provider.get("source") != "agent_runtime_session_events":
                self._mark_usage_manual_review(
                    client,
                    reservation,
                    now,
                    reason="no_supported_provider_usage_recovery_source",
                )
                skipped += 1
                continue
            raw_usage = self._usage_hydrator.retrieve_usage(
                resource_name=str(provider.get("resource_name") or ""),
                region=str(provider.get("region") or ""),
                session_id=str(ledger.get("session_id") or ""),
                invocation_id=(
                    str(provider.get("invocation_id") or "") or None
                ),
                started_at=_as_utc_or_none(reservation.get("created_at")),
                completed_at=_as_utc_or_none(ledger.get("created_at")),
                model_name=(str(provider.get("model_name") or "") or None),
                pricing_model=(str(provider.get("pricing_model") or "") or None),
                model_endpoint_location=(
                    str(provider.get("model_endpoint_location") or "") or None
                ),
            )
            if not isinstance(raw_usage, dict):
                self._schedule_usage_recovery_retry(client, reservation, now)
                skipped += 1
                continue
            cost_nanos = _cost_usd_nanos(raw_usage.get("estimated_cost_usd"))
            if cost_nanos is None:
                self._mark_usage_manual_review(
                    client,
                    reservation,
                    now,
                    reason="recovered_usage_unpriced",
                )
                skipped += 1
                continue
            recovered_usage = dict(raw_usage)
            recovered_usage["estimated_cost_usd_nanos"] = cost_nanos
            reservation_ref = client.collection(
                self._settings.billing_reservations_collection
            ).document(turn_id)
            reservation_ref.update(
                {
                    "recovered_usage": recovered_usage,
                    "usage_recovered_at": now,
                    "usage_reconciliation_status": "usage_recovered",
                    "updated_at": now,
                }
            )
            thread_id = str(ledger.get("thread_id") or "").strip()
            if thread_id:
                try:
                    self._messages_repository.update_assistant_usage(
                        client,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        usage=recovered_usage,
                        recovered_at=now,
                    )
                except Exception as exc:
                    LOGGER.warning(
                        "Recovered Agent Runtime usage could not update the assistant transcript (%s).",
                        type(exc).__name__,
                    )
            reservation["recovered_usage"] = recovered_usage
            event = _event_from_ledger_and_reservation(ledger, reservation)
            try:
                result = self._settlement_service.settle_if_required_sync(event)
            except RetryableWorkerError:
                self._schedule_usage_recovery_retry(client, reservation, now)
                skipped += 1
                continue
            if result is None or result.status in {
                "usage_reconciliation_pending",
                "usage_recovery_manual_review",
            }:
                self._schedule_usage_recovery_retry(client, reservation, now)
                skipped += 1
            else:
                if result.shortfall_nanos:
                    LOGGER.error(
                        "Recovered Agent Runtime usage exceeds available wallet credit for turn %s; manual billing review is required.",
                        turn_id,
                    )
                settled += 1

        v4_scanned, v4_settled, v4_skipped = self._reconcile_v4_pending_sync(
            client, now
        )
        scanned += v4_scanned
        settled += v4_settled
        skipped += v4_skipped

        # Batch close is intentionally part of the same hourly worker endpoint:
        # turn persistence only queues estimates; this transaction performs
        # the wallet debit and records any unpaid remainder as shared debt.
        batch_result = self._batch_settlement_service.reconcile_due_sync()
        settled += batch_result.settled_turns
        skipped += batch_result.skipped_entries
        return BillingReconciliationResult(
            scanned_reservations=scanned,
            settled_reservations=settled,
            released_reservations=released,
            skipped_reservations=skipped,
        )

    def _reconcile_v4_pending_sync(self, client: Any, now: datetime) -> tuple[int, int, int]:
        snapshots = list(self._v4_pending_markers(client, now))
        scanned = settled = skipped = usage_lookups = 0
        for snapshot in snapshots:
            reservation = snapshot.to_dict() or {}
            status = reservation.get("status")
            turn_id = _required_string(reservation.get("turn_id"), "turn_id")
            ledger_ref = client.collection(
                self._settings.billing_ledger_collection
            ).document(turn_id)
            ledger_snapshot = ledger_ref.get()
            scanned += 1
            if not ledger_snapshot.exists:
                self._mark_usage_manual_review(
                    client,
                    reservation,
                    now,
                    reason="billing_ledger_missing",
                )
                skipped += 1
                continue
            ledger = ledger_snapshot.to_dict() or {}
            event = _event_from_ledger_and_reservation(ledger, reservation)

            if status == "completed_pending":
                usage = event.usage if isinstance(event.usage, dict) else {}
                if (
                    _ledger_cost_nanos(ledger) is None
                    or has_pending_provider_usage(usage)
                ):
                    self._schedule_usage_recovery_retry(client, reservation, now)
                    reservation_ref = client.collection(
                        self._settings.billing_reservations_collection
                    ).document(turn_id)
                    reservation_ref.update(
                        {
                            "status": "late_usage_pending",
                            "usage_reconciliation_status": "pending",
                            "usage_reconciliation_started_at": now,
                            "usage_reconciliation_deadline_at": now
                            + timedelta(
                                seconds=max(
                                    1,
                                    self._settings.billing_usage_reconciliation_retention_seconds,
                                )
                            ),
                            "usage_reconciliation_next_attempt_at": now
                            + timedelta(
                                seconds=max(
                                    1,
                                    self._settings.billing_usage_reconciliation_initial_backoff_seconds,
                                )
                            ),
                            "updated_at": now,
                        }
                    )
                    skipped += 1
                    continue
                try:
                    if self._settings.billing_batch_settlement_enabled:
                        self._batch_settlement_service.enqueue_turn_sync(event)
                    else:
                        result = self._settlement_service.settle_if_required_sync(event)
                        if result is not None:
                            settled += 1
                except RetryableWorkerError:
                    skipped += 1
                continue

            if status != "late_usage_pending":
                continue
            deadline = _as_utc_or_none(
                reservation.get("usage_reconciliation_deadline_at")
            )
            if deadline is not None and now >= deadline:
                self._mark_usage_manual_review(
                    client,
                    reservation,
                    now,
                    reason="provider_usage_retention_expired",
                )
                skipped += 1
                continue
            next_attempt = _as_utc_or_none(
                reservation.get("usage_reconciliation_next_attempt_at")
            )
            if next_attempt is not None and now < next_attempt:
                skipped += 1
                continue
            if usage_lookups >= MAX_USAGE_LOOKUPS_PER_PASS:
                skipped += 1
                continue

            provider = ledger.get("provider_usage_recovery")
            provider = provider if isinstance(provider, dict) else {}
            if provider.get("source") != "agent_runtime_session_events":
                self._mark_usage_manual_review(
                    client,
                    reservation,
                    now,
                    reason="no_supported_provider_usage_recovery_source",
                )
                skipped += 1
                continue

            raw_usage = reservation.get("recovered_usage")
            if (
                not isinstance(raw_usage, dict)
                or has_pending_provider_usage(raw_usage)
                or _cost_usd_nanos(raw_usage.get("estimated_cost_usd")) is None
            ):
                usage_lookups += 1
                raw_usage = self._usage_hydrator.retrieve_usage(
                    resource_name=str(provider.get("resource_name") or ""),
                    region=str(provider.get("region") or ""),
                    session_id=str(ledger.get("session_id") or ""),
                    invocation_id=str(provider.get("invocation_id") or "") or None,
                    started_at=(
                        _as_utc_or_none(provider.get("started_at"))
                        or _as_utc_or_none(reservation.get("created_at"))
                    ),
                    completed_at=_as_utc_or_none(ledger.get("created_at")),
                    model_name=str(provider.get("model_name") or "") or None,
                    pricing_model=str(provider.get("pricing_model") or "") or None,
                    model_endpoint_location=(
                        str(provider.get("model_endpoint_location") or "") or None
                    ),
                )
            if not isinstance(raw_usage, dict):
                self._schedule_usage_recovery_retry(client, reservation, now)
                skipped += 1
                continue
            cost_nanos = _cost_usd_nanos(raw_usage.get("estimated_cost_usd"))
            if (
                cost_nanos is None
                or raw_usage.get("usage_reporting_complete") is not True
            ):
                self._schedule_usage_recovery_retry(client, reservation, now)
                skipped += 1
                continue
            recovered_usage = dict(raw_usage)
            recovered_usage["estimated_cost_usd_nanos"] = cost_nanos
            try:
                self._ledger_repository.update_recovered_usage(
                    client,
                    turn_id=turn_id,
                    usage=recovered_usage,
                    recovered_at=now,
                )
            except Exception as exc:
                LOGGER.warning(
                    "Recovered Agent Runtime usage could not update ledger %s (%s).",
                    turn_id,
                    type(exc).__name__,
                )
                self._schedule_usage_recovery_retry(client, reservation, now)
                skipped += 1
                continue
            reservation_ref = client.collection(
                self._settings.billing_reservations_collection
            ).document(turn_id)
            reservation_ref.update(
                {
                    "recovered_usage": recovered_usage,
                    "usage_recovered_at": now,
                    "usage_reconciliation_status": "usage_recovered",
                    "updated_at": now,
                }
            )
            thread_id = str(ledger.get("thread_id") or "").strip()
            if thread_id:
                try:
                    self._messages_repository.update_assistant_usage(
                        client,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        usage=recovered_usage,
                        recovered_at=now,
                    )
                except Exception as exc:
                    LOGGER.warning(
                        "Recovered Agent Runtime usage could not update assistant transcript (%s).",
                        type(exc).__name__,
                    )
            ledger.update(
                {
                    "usage": recovered_usage,
                    "estimated_cost_usd": raw_usage.get("estimated_cost_usd"),
                    "estimated_cost_usd_nanos": cost_nanos,
                    "cost_status": "estimated",
                }
            )
            event = _event_from_ledger_and_reservation(
                ledger, {**reservation, "recovered_usage": recovered_usage}
            )
            try:
                if self._settings.billing_batch_settlement_enabled:
                    self._batch_settlement_service.enqueue_turn_sync(
                        event, late_usage=True
                    )
                else:
                    result = self._settlement_service.settle_if_required_sync(event)
                    if result is not None:
                        settled += 1
            except RetryableWorkerError:
                # The ledger correction and marker are durable; the next pass
                # will reuse `recovered_usage` and retry the idempotent enqueue.
                self._schedule_usage_recovery_retry(client, reservation, now)
                skipped += 1
        return scanned, settled, skipped

    def _v4_pending_markers(self, client: Any, now: datetime):
        collection = client.collection(self._settings.billing_reservations_collection)
        limit = max(1, self._settings.billing_reconciliation_batch_size)
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter

            completed = list(
                collection.where(filter=FieldFilter("status", "==", "completed_pending"))
                .order_by("billing_batch_created_at")
                .limit(limit)
                .stream()
            )
            late = list(
                collection.where(filter=FieldFilter("status", "==", "late_usage_pending"))
                .where(filter=FieldFilter("usage_reconciliation_next_attempt_at", "<=", now))
                .order_by("usage_reconciliation_next_attempt_at")
                .limit(min(limit, MAX_USAGE_LOOKUPS_PER_PASS))
                .stream()
            )
            return completed + late
        except (AttributeError, ModuleNotFoundError, ImportError):
            rows = [
                snapshot
                for snapshot in collection.stream()
                if (snapshot.to_dict() or {}).get("status")
                in {"completed_pending", "late_usage_pending"}
            ]
            rows.sort(
                key=lambda snapshot: _as_utc_or_none(
                    (snapshot.to_dict() or {}).get("billing_batch_created_at")
                )
                or datetime.min.replace(tzinfo=timezone.utc)
            )
            return rows[:limit]

    def _expired_reservations(self, client: Any, now: datetime):
        collection = client.collection(self._settings.billing_reservations_collection)
        batch_size = self._settings.billing_reconciliation_batch_size
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter

            return (
                collection.where(filter=FieldFilter("status", "==", "reserved"))
                .where(filter=FieldFilter("expires_at", "<=", now))
                .order_by("expires_at")
                .limit(batch_size)
                .stream()
            )
        except (AttributeError, ModuleNotFoundError, ImportError):
            # Test doubles and older fakes may not implement Firestore queries.
            # Keep the same status filter before applying the bounded limit.
            candidates = []
            for snapshot in collection.stream():
                reservation = snapshot.to_dict() or {}
                expires_at = reservation.get("expires_at")
                if (
                    reservation.get("status") == "reserved"
                    and isinstance(expires_at, datetime)
                    and expires_at <= now
                ):
                    candidates.append((expires_at, snapshot))
            candidates.sort(key=lambda item: item[0])
            return [snapshot for _, snapshot in candidates[:batch_size]]

    def _pending_usage_reservations(self, client: Any, now: datetime):
        collection = client.collection(self._settings.billing_reservations_collection)
        batch_size = min(
            self._settings.billing_reconciliation_batch_size,
            MAX_USAGE_LOOKUPS_PER_PASS,
        )
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter

            return (
                collection.where(
                    filter=FieldFilter("status", "==", "usage_reconciliation_pending")
                )
                .order_by("usage_reconciliation_next_attempt_at")
                .limit(batch_size)
                .stream()
            )
        except (AttributeError, ModuleNotFoundError, ImportError):
            candidates = []
            for snapshot in collection.stream():
                reservation = snapshot.to_dict() or {}
                next_attempt = _as_utc_or_none(
                    reservation.get("usage_reconciliation_next_attempt_at")
                )
                if reservation.get("status") == "usage_reconciliation_pending":
                    candidates.append(
                        (next_attempt or datetime.min.replace(tzinfo=timezone.utc), snapshot)
                    )
            candidates.sort(key=lambda item: item[0])
            return [snapshot for _, snapshot in candidates[:batch_size]]

    def _manual_review_reservations(self, client: Any):
        """Return a bounded oldest-first batch for migration of legacy held cases."""
        collection = client.collection(self._settings.billing_reservations_collection)
        batch_size = self._settings.billing_reconciliation_batch_size
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter

            return (
                collection.where(
                    filter=FieldFilter("status", "==", "usage_recovery_manual_review")
                )
                .order_by("updated_at")
                .limit(batch_size)
                .stream()
            )
        except (AttributeError, ModuleNotFoundError, ImportError):
            candidates = []
            for snapshot in collection.stream():
                reservation = snapshot.to_dict() or {}
                if reservation.get("status") == "usage_recovery_manual_review":
                    updated_at = _as_utc_or_none(reservation.get("updated_at"))
                    candidates.append(
                        (updated_at or datetime.min.replace(tzinfo=timezone.utc), snapshot)
                    )
            candidates.sort(key=lambda item: item[0])
            return [snapshot for _, snapshot in candidates[:batch_size]]

    def _schedule_usage_recovery_retry(
        self,
        client: Any,
        reservation: dict[str, Any],
        now: datetime,
    ) -> None:
        turn_id = _required_string(reservation.get("turn_id"), "turn_id")
        attempts = _nonnegative_count(
            reservation.get("usage_reconciliation_attempt_count"), default=0
        )
        initial_delay = max(
            1, self._settings.billing_usage_reconciliation_initial_backoff_seconds
        )
        maximum_delay = max(
            initial_delay,
            self._settings.billing_usage_reconciliation_max_backoff_seconds,
        )
        delay_seconds = min(initial_delay * (2 ** min(attempts, 20)), maximum_delay)
        next_attempt = now + timedelta(seconds=delay_seconds)
        deadline = _as_utc_or_none(
            reservation.get("usage_reconciliation_deadline_at")
        )
        if deadline is not None:
            next_attempt = min(next_attempt, deadline)
        client.collection(self._settings.billing_reservations_collection).document(
            turn_id
        ).update(
            {
                "usage_reconciliation_status": "pending",
                "usage_reconciliation_attempt_count": attempts + 1,
                "usage_reconciliation_next_attempt_at": next_attempt,
                "updated_at": now,
            }
        )

    def _mark_usage_manual_review(
        self,
        client: Any,
        reservation: dict[str, Any],
        now: datetime,
        *,
        reason: str,
    ) -> None:
        turn_id = _required_string(reservation.get("turn_id"), "turn_id")
        client.collection(self._settings.billing_reservations_collection).document(
            turn_id
        ).update(
            {
                "status": "usage_recovery_manual_review",
                "usage_reconciliation_status": "manual_review",
                "usage_reconciliation_manual_review_reason": reason,
                "usage_reconciliation_manual_review_at": now,
                "usage_reconciliation_next_attempt_at": None,
                "updated_at": now,
            }
        )
        LOGGER.error(
            "Agent Runtime usage recovery requires manual billing review for turn %s (%s).",
            turn_id,
            reason,
        )

    def _hold_expired_reservation_for_review_sync(
        self,
        client: Any,
        reservation: dict[str, Any],
        reviewed_at: datetime,
    ) -> bool:
        """Release expired orphan holds while flagging missing-ledger charges for review.

        A missing completion ledger does not prove the provider never ran, so
        keep the discrepancy visible for manual billing review. The unresolved
        record must not keep reducing a customer's spendable balance, though.
        """
        turn_id = _required_string(reservation.get("turn_id"), "turn_id")
        billing_subject_id = _required_string(
            reservation.get("billing_subject_id"),
            "billing_subject_id",
        )
        owner_uid = _required_string(reservation.get("owner_uid"), "owner_uid")
        reservation_ref = client.collection(
            self._settings.billing_reservations_collection
        ).document(turn_id)
        wallet_ref = client.collection(self._settings.wallets_collection).document(
            customer_wallet_document_id(billing_subject_id)
        )

        def operation(transaction: Any) -> bool:
            # Keep all reads before writes for Firestore transaction validity.
            current_reservation_snapshot = get_transaction_document_snapshot(
                transaction, reservation_ref
            )
            wallet_snapshot = get_transaction_document_snapshot(transaction, wallet_ref)
            if not current_reservation_snapshot.exists:
                return False
            current_reservation = current_reservation_snapshot.to_dict() or {}
            if current_reservation.get("status") not in {
                "reserved",
                "usage_reconciliation_pending",
                "usage_recovery_manual_review",
            }:
                return False
            if current_reservation.get("usage_recovery_hold_released"):
                return False
            hold_already_registered = bool(
                current_reservation.get("usage_recovery_hold_registered")
            )
            if not wallet_snapshot.exists:
                raise RetryableWorkerError(
                    f"Wallet is missing for ambiguous expired reservation {turn_id}."
                )
            wallet = wallet_snapshot.to_dict() or {}
            if (
                wallet.get("status") != "active"
                or wallet.get("billing_subject_id") != billing_subject_id
                or wallet.get("owner_uid") != owner_uid
            ):
                raise RetryableWorkerError(
                    f"Wallet does not match ambiguous expired reservation {turn_id}."
                )
            try:
                reserved_amount_nanos = nonnegative_int(
                    current_reservation.get("reserved_amount_nanos"),
                    field_name="reserved_amount_nanos",
                )
                reserved_credit_nanos = nonnegative_int(
                    wallet.get("reserved_credit_nanos"),
                    field_name="reserved_credit_nanos",
                )
                pending_usage_count = nonnegative_int(
                    wallet.get("pending_usage_reconciliation_count", 0),
                    field_name="pending_usage_reconciliation_count",
                )
                available_credit_nanos = nonnegative_int(
                    wallet.get("available_credit_nanos", 0),
                    field_name="available_credit_nanos",
                )
            except ValueError as exc:
                raise RetryableWorkerError(
                    f"Wallet data is invalid for ambiguous expired reservation {turn_id}."
                ) from exc
            if reserved_credit_nanos < reserved_amount_nanos:
                raise RetryableWorkerError(
                    f"Wallet reserved credit is invalid for ambiguous expired reservation {turn_id}."
                )
            transaction.update(
                wallet_ref,
                {
                    "available_credit_nanos": (
                        available_credit_nanos + reserved_amount_nanos
                    ),
                    "reserved_credit_nanos": (
                        reserved_credit_nanos - reserved_amount_nanos
                    ),
                    "pending_usage_reconciliation_count": (
                        pending_usage_count
                        if hold_already_registered
                        else pending_usage_count + 1
                    ),
                    "updated_at": reviewed_at,
                },
            )
            transaction.update(
                reservation_ref,
                {
                    "status": "usage_recovery_manual_review",
                    "usage_reconciliation_status": "manual_review",
                    "usage_recovery_hold_registered": True,
                    "usage_recovery_hold_released": True,
                    "usage_reconciliation_manual_review_reason": (
                        current_reservation.get(
                            "usage_reconciliation_manual_review_reason"
                        )
                        or (
                            "completed_turn_ledger_missing_after_reservation_expiry"
                            if current_reservation.get("status") == "reserved"
                            else "billing_ledger_missing"
                        )
                    ),
                    "usage_reconciliation_manual_review_at": reviewed_at,
                    "usage_reconciliation_next_attempt_at": None,
                    "updated_at": reviewed_at,
                },
            )
            return True

        held = _run_firestore_transaction(client, operation)
        if held:
            LOGGER.error(
                "Reservation for turn %s has no completion ledger; its wallet hold was released and the possible charge requires manual billing review.",
                turn_id,
            )
        return held


def _event_from_ledger_and_reservation(
    ledger: dict[str, Any],
    reservation: dict[str, Any],
) -> TurnCompletedEvent:
    usage = reservation.get("recovered_usage")
    if not isinstance(usage, dict):
        usage = ledger.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    provider_usage_recovery = ledger.get("provider_usage_recovery")
    if not isinstance(provider_usage_recovery, dict):
        provider_usage_recovery = {}
    return TurnCompletedEvent(
        event_id=_required_string(ledger.get("event_id"), "event_id"),
        turn_id=_required_string(ledger.get("turn_id"), "turn_id"),
        agent_id=_required_string(ledger.get("agent_id"), "agent_id"),
        user_id=_required_string(ledger.get("uid"), "uid"),
        thread_id=_required_string(ledger.get("thread_id"), "thread_id"),
        session_id=_required_string(ledger.get("session_id"), "session_id"),
        user_message="",
        assistant_message="",
        created_at=_required_datetime(ledger.get("created_at"), "created_at"),
        usage=usage,
        metadata={
            "billing": {
                "reservation_id": reservation.get("reservation_id"),
                "billing_subject_id": reservation.get("billing_subject_id"),
                "request_id": reservation.get("request_id"),
                "minimum_balance_nanos": reservation.get("minimum_balance_nanos", 0),
                "reserved_amount_nanos": 0,
            },
            "provider_usage_recovery": provider_usage_recovery,
            "provider_invocation_id": ledger.get("provider_invocation_id"),
            "provider": ledger.get("provider", {}),
        },
        infrastructure_metering=None,
    )


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RetryableWorkerError(f"Billing reconciliation field {field_name} is required.")
    return value.strip()


def _required_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise RetryableWorkerError(f"Billing reconciliation field {field_name} is required.")
    return value


def _ledger_cost_nanos(ledger: dict[str, Any]) -> int | None:
    value = ledger.get("estimated_cost_usd_nanos")
    if value is None:
        return None
    try:
        return nonnegative_int(value, field_name="estimated_cost_usd_nanos")
    except ValueError as exc:
        raise RetryableWorkerError("Billing ledger has an invalid estimated cost.") from exc


def _cost_usd_nanos(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return int(
        (amount * Decimal("1000000000")).to_integral_value(rounding=ROUND_HALF_UP)
    )


def _as_utc_or_none(value: Any) -> datetime | None:
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            value = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _nonnegative_count(value: Any, *, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return max(0, default)
    return value


def _run_firestore_transaction(client: Any, operation: Callable[[Any], bool]) -> bool:
    from google.cloud import firestore

    transaction = client.transaction()

    @firestore.transactional
    def run(transaction: Any) -> bool:
        return operation(transaction)

    return run(transaction)
