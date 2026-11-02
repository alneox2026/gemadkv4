from common.schemas import ChatRequest, InfrastructureMetering, TurnCompletedEvent


def test_chat_request_trims_message() -> None:
    payload = ChatRequest(message="  hello  ")
    assert payload.message == "hello"


def test_turn_completed_event_accepts_minimal_payload() -> None:
    event = TurnCompletedEvent(
        event_id="evt-1",
        turn_id="turn-1",
        agent_id="maxima",
        user_id="user-1",
        thread_id="thread-1",
        session_id="session-1",
        user_message="hello",
        assistant_message="hi",
    )
    assert event.agent_id == "maxima"
    assert event.infrastructure_metering is None


def test_infrastructure_metering_is_explicitly_shadow_only() -> None:
    meter = InfrastructureMetering(
        deployment_environment="development",
        project_id="test-project",
        gateway_region="us-central1",
        billing_subject_id="user-1",
        root_request_id="req-server-generated",
        agent_backend="agent_runtime",
        agent_region="us-central1",
        gateway_wall_duration_ms=4200,
        assistant_content_bytes=8,
    )

    assert meter.mode == "shadow"
    assert meter.gateway_wall_duration_ms == 4200
    assert meter.assistant_content_bytes == 8


def test_agent_config_effective_model_endpoint_location() -> None:
    from common.schemas import AgentConfig

    # Cloud Run ADK without explicit model_endpoint_location defaults to "global"
    adk_default = AgentConfig(
        agent_id="adk_agent",
        backend="cloud_run_adk",
        base_url="https://test.us-central1.run.app",
        app_name="app",
        region="us-central1",
    )
    assert adk_default.effective_model_endpoint_location == "global"

    # Cloud Run ADK with explicit model_endpoint_location
    adk_explicit = AgentConfig(
        agent_id="adk_agent_2",
        backend="cloud_run_adk",
        base_url="https://test.us-central1.run.app",
        app_name="app",
        region="us-central1",
        model_endpoint_location="us-central1",
    )
    assert adk_explicit.effective_model_endpoint_location == "us-central1"

    # Agent Runtime without explicit model_endpoint_location does NOT infer from resource_name; defaults to global
    ar_agent = AgentConfig(
        agent_id="ar_agent",
        backend="agent_runtime",
        resource_name="projects/test/locations/us-central1/reasoningEngines/123",
        region="us-central1",
    )
    assert ar_agent.effective_model_endpoint_location == "global"

    # Agent Runtime with explicit model_endpoint_location
    ar_agent_explicit = AgentConfig(
        agent_id="ar_agent_explicit",
        backend="agent_runtime",
        resource_name="projects/test/locations/us-central1/reasoningEngines/123",
        region="us-central1",
        model_endpoint_location="us-central1",
    )
    assert ar_agent_explicit.effective_model_endpoint_location == "us-central1"

