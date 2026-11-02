from datetime import datetime, timezone
import time

from common.schemas import AgentConfig, ChatRequest
from services.agent_gateway_v3.app.services.request_context import RequestContext
from services.agent_gateway_v3.app.services.turn_event_builder import (
    build_turn_completed_event,
)


def test_turn_event_builder_attaches_server_derived_shadow_meter() -> None:
    agent = AgentConfig(
        agent_id="maxima",
        backend="agent_runtime",
        model="gemini-3.8-flash",
        resource_name="projects/test/locations/us-central1/reasoningEngines/123",
        region="us-central1",
    )
    context = RequestContext(
        request_id="req-server-generated",
        turn_id="turn-1",
        started_at=datetime.now(timezone.utc),
        agent_id="maxima",
        started_monotonic_ns=time.perf_counter_ns() - 50_000_000,
    )
    event = build_turn_completed_event(
        request_context=context,
        agent_config=agent,
        user_id="verified-user-1",
        payload=ChatRequest(message="hello"),
        thread_id="thread-1",
        session_id="session-1",
        assistant_message="hi 🌍",
        billing_metadata={"billing_subject_id": "server-payer-1"},
        provider_invocation_id="invocation-123",
    )

    assert event.user_id == "verified-user-1"
    assert event.infrastructure_metering is not None
    assert event.infrastructure_metering.mode == "shadow"
    assert event.infrastructure_metering.billing_subject_id == "server-payer-1"
    assert event.infrastructure_metering.root_request_id == "req-server-generated"
    assert event.infrastructure_metering.agent_backend == "agent_runtime"
    assert event.infrastructure_metering.project_id
    assert event.infrastructure_metering.gateway_region
    assert event.infrastructure_metering.gateway_wall_duration_ms >= 50
    assert event.infrastructure_metering.assistant_content_bytes == len(
        "hi 🌍".encode("utf-8")
    )
    assert event.metadata["provider_usage_recovery"] == {
        "source": "agent_runtime_session_events",
        "resource_name": "projects/test/locations/us-central1/reasoningEngines/123",
        "region": "us-central1",
        "invocation_id": "invocation-123",
        "model_name": "gemini-3.8-flash",
        "pricing_model": None,
        "model_endpoint_location": "global",
        "started_at": context.started_at.isoformat(),
    }
