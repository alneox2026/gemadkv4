"""Worker-owned, zero-hold lifecycle markers for completed turns."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from common.schemas import TurnCompletedEvent
from services.agent_persistence_worker_v3.app.core.config import get_settings
from services.agent_persistence_worker_v3.app.core.errors import RetryableWorkerError


class BillingReservationRepository:
    """Creates the turn lifecycle marker atomically with its immutable ledger.

    The legacy collection name is retained for schema compatibility, but new
    documents are not wallet holds: the gateway performs a read-only minimum
    balance check, and estimates are not subtracted from wallet balance.
    """

    def __init__(self, collection: str | None = None) -> None:
        self._collection = collection or get_settings().billing_reservations_collection
        self._settings = get_settings()

    def add_create_to_batch(
        self, batch: Any, client: Any, event: TurnCompletedEvent
    ) -> None:
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        billing = metadata.get("billing")
        if not isinstance(billing, dict):
            return
        turn_id = str(event.turn_id)
        reservation_id = str(
            billing.get("reservation_id") or billing.get("billing_reservation_id") or ""
        ).strip()
        subject_id = str(billing.get("billing_subject_id") or "").strip()
        if reservation_id != turn_id or subject_id != event.user_id:
            raise RetryableWorkerError(
                f"Billing metadata does not match completed turn {turn_id}."
            )

        now = datetime.now(timezone.utc)
        usage = event.usage if isinstance(event.usage, dict) else {}
        provider_pending = has_pending_provider_usage(usage) or (
            _cost_usd_nanos(usage.get("estimated_cost_usd")) is None
        )
        data: dict[str, Any] = {
            "schema_version": 1,
            "reservation_id": turn_id,
            "turn_id": turn_id,
            "request_id": billing.get("request_id"),
            "agent_id": event.agent_id,
            "session_id": event.session_id,
            "thread_id": event.thread_id,
            "owner_uid": event.user_id,
            "billing_subject_id": subject_id,
            "currency": "USD",
            "reserved_amount_nanos": 0,
            "minimum_balance_nanos": _nonnegative_int(
                billing.get("minimum_balance_nanos")
            ),
            "authorization_mode": "minimum_balance_check",
            "status": "late_usage_pending" if provider_pending else "completed_pending",
            "created_at": event.created_at or now,
            "turn_created_at": event.created_at or now,
            "billing_batch_created_at": now,
            "updated_at": now,
        }
        if provider_pending:
            data.update(
                {
                    "usage_reconciliation_status": "pending",
                    "usage_reconciliation_attempt_count": 0,
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
                    "usage_recovery_hold_registered": False,
                }
            )
        batch.create(
            client.collection(self._collection).document(turn_id),
            data,
        )


def has_pending_provider_usage(
    usage: dict[str, Any], *, provider_name: Any = None
) -> bool:
    # `provider_name` remains accepted for older callers, but all backends now
    # use the same completeness rule. An estimate alone is not proof that the
    # provider returned a complete input/output/total token report.
    del provider_name
    if (
        usage.get("billing_status") == "pending_provider_usage"
        or usage.get("usage_reporting_complete") is False
    ):
        return True
    reports = usage.get("raw_turn_usage_reports")
    if isinstance(reports, list):
        return any(
            isinstance(report, dict)
            and not _has_complete_usage(report.get("usage"))
            for report in reports
        )
    return not _has_complete_usage(usage)


def _has_complete_usage(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    # The worker's token-rate estimate can exist even when the provider's
    # reported token breakdown is incomplete. Do not mistake that estimate for
    # finalized provider usage; reconciliation must keep retrying the saved
    # interaction until input, output, and total counts are all available.
    aliases = (
        ("prompt_token_count", "promptTokenCount", "total_input_tokens", "totalInputTokens"),
        ("candidates_token_count", "candidatesTokenCount", "total_output_tokens", "totalOutputTokens"),
        ("total_token_count", "totalTokenCount", "total_tokens", "totalTokens"),
    )
    token_counts = value.get("token_counts")
    counts = token_counts if isinstance(token_counts, dict) else value
    return all(
        any(
            isinstance(counts.get(key), int)
            and not isinstance(counts.get(key), bool)
            and counts.get(key) >= 0
            for key in alternatives
        )
        for alternatives in aliases
    )


def _cost_usd_nanos(value: Any) -> int | None:
    from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return int((amount * Decimal("1000000000")).to_integral_value(rounding=ROUND_HALF_UP))


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


