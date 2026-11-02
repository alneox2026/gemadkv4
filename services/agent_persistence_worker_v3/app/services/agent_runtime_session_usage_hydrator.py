"""Recover delayed ADK usage from persisted Agent Runtime session events."""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import google.auth
import httpx
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request as GoogleAuthRequest

from services.agent_gateway_v3.app.services.usage_metadata import (
    extract_usage_metadata,
    normalize_usage_metadata,
)

LOGGER = logging.getLogger(__name__)
AUTH_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
MAX_EVENT_PAGES = 2
EVENT_PAGE_SIZE = 100


class AgentRuntimeSessionUsageHydrator:
    """Read usage for one invocation from its persisted Agent Runtime session."""

    def __init__(
        self,
        *,
        http_client: httpx.Client | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._http_client = http_client
        self._timeout_seconds = timeout_seconds
        self._credentials: Any = None

    def retrieve_usage(
        self,
        *,
        resource_name: str,
        region: str,
        session_id: str,
        invocation_id: str | None,
        started_at: datetime | None,
        completed_at: datetime | None,
        model_name: str | None,
        pricing_model: str | None,
        model_endpoint_location: str | None,
    ) -> dict[str, Any] | None:
        resource_name = resource_name.strip().strip("/")
        region = region.strip()
        session_id = session_id.strip()
        if (
            not resource_name.startswith("projects/")
            or "/reasoningEngines/" not in resource_name
            or not region
            or not session_id
            or started_at is None
            or completed_at is None
        ):
            return None

        client = self._http_client or httpx.Client(
            timeout=httpx.Timeout(self._timeout_seconds, connect=3.0)
        )
        try:
            headers = self._auth_headers()
            resource_path = quote(resource_name, safe="/")
            encoded_session = quote(session_id, safe="")
            url = (
                f"https://{region}-aiplatform.googleapis.com/v1beta1/"
                f"{resource_path}/sessions/{encoded_session}/events"
            )
            start = _as_utc(started_at) - timedelta(seconds=30)
            end = _as_utc(completed_at) + timedelta(seconds=30)
            params: dict[str, Any] = {
                "pageSize": EVENT_PAGE_SIZE,
                "filter": (
                    f'timestamp>="{_format_timestamp(start)}" AND '
                    f'timestamp<="{_format_timestamp(end)}"'
                ),
            }
            events: list[dict[str, Any]] = []
            for page_number in range(MAX_EVENT_PAGES):
                response = client.get(url, headers=headers, params=params)
                if response.status_code != 200:
                    LOGGER.warning(
                        "Agent Runtime session usage lookup returned HTTP %s.",
                        response.status_code,
                    )
                    return None
                payload = response.json()
                if not isinstance(payload, dict):
                    return None
                page = payload.get("sessionEvents", payload.get("session_events", []))
                if isinstance(page, list):
                    events.extend(item for item in page if isinstance(item, dict))
                page_token = payload.get("nextPageToken") or payload.get("next_page_token")
                if not page_token:
                    break
                if page_number == MAX_EVENT_PAGES - 1:
                    # Never price a partial event set: usage can span multiple
                    # model calls inside one ADK invocation.
                    LOGGER.warning(
                        "Agent Runtime usage lookup exceeded the event page limit."
                    )
                    return None
                params["pageToken"] = page_token

            requested_invocation = (invocation_id or "").strip()
            candidate_events = []
            candidate_invocations: set[str] = set()
            for event in events:
                event_invocation = str(
                    event.get("invocationId") or event.get("invocation_id") or ""
                ).strip()
                if event_invocation:
                    candidate_invocations.add(event_invocation)
                if requested_invocation and event_invocation != requested_invocation:
                    continue
                candidate_events.append(event)
            if not requested_invocation and len(candidate_invocations) != 1:
                LOGGER.warning(
                    "Agent Runtime usage lookup is ambiguous: the turn has no invocation ID and %s invocation IDs matched its time window.",
                    len(candidate_invocations),
                )
                return None
            if not requested_invocation:
                only_invocation = next(iter(candidate_invocations))
                candidate_events = [
                    event
                    for event in candidate_events
                    if str(event.get("invocationId") or event.get("invocation_id") or "").strip()
                    == only_invocation
                ]

            raw_usages = [
                usage
                for event in candidate_events
                if (usage := _event_usage(event)) is not None
            ]
            if not raw_usages:
                return None
            normalized_usages = [
                normalize_usage_metadata(
                    usage,
                    model_name=model_name,
                    pricing_model=pricing_model,
                    endpoint_location=model_endpoint_location,
                )
                for usage in raw_usages
            ]
            # A turn may contain several model calls. Never bill a partial
            # subset of the session events: every usage-bearing event for the
            # selected invocation must have complete input/output/total counts.
            if any(
                usage.get("usage_reporting_complete") is not True
                for usage in normalized_usages
            ):
                return None
            priced = [
                usage
                for usage in normalized_usages
                if usage.get("estimated_cost_usd") is not None
            ]
            if not priced:
                return None
            return _aggregate_normalized_usage(priced)
        except (GoogleAuthError, httpx.RequestError, ValueError, TypeError) as exc:
            LOGGER.warning(
                "Agent Runtime session usage lookup failed (%s).",
                type(exc).__name__,
            )
            return None
        finally:
            if self._http_client is None:
                client.close()

    def _auth_headers(self) -> dict[str, str]:
        if self._credentials is None:
            self._credentials, _ = google.auth.default(scopes=AUTH_SCOPES)
        if not self._credentials.valid or not self._credentials.token:
            self._credentials.refresh(GoogleAuthRequest())
        return {
            "Authorization": f"Bearer {self._credentials.token}",
            "Accept": "application/json",
        }


def _event_usage(event: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("rawEvent", "raw_event"):
        raw_event = event.get(key)
        if isinstance(raw_event, dict):
            usage = extract_usage_metadata(raw_event)
            if usage is not None:
                return usage
    usage = extract_usage_metadata(event)
    if usage is not None:
        return usage
    event_metadata = event.get("eventMetadata") or event.get("event_metadata")
    if isinstance(event_metadata, dict):
        custom_metadata = event_metadata.get("customMetadata") or event_metadata.get(
            "custom_metadata"
        )
        if isinstance(custom_metadata, dict):
            usage = custom_metadata.get("_usage_metadata") or custom_metadata.get(
                "_usageMetadata"
            )
            if isinstance(usage, dict):
                return usage
    return None


def _aggregate_normalized_usage(usages: list[dict[str, Any]]) -> dict[str, Any]:
    aggregate = deepcopy(usages[0])
    for field in ("token_counts", "billable_tokens", "estimated_cost_breakdown_usd"):
        totals: dict[str, int | float] = {}
        for usage in usages:
            values = usage.get(field)
            if not isinstance(values, dict):
                continue
            for key, value in values.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                totals[key] = totals.get(key, 0) + value
        if totals:
            aggregate[field] = totals
    aggregate["estimated_cost_usd"] = round(
        sum(float(usage["estimated_cost_usd"]) for usage in usages), 9
    )
    return aggregate


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
