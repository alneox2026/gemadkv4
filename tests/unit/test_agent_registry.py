import pytest

from services.agent_gateway_v3.app.core.config import get_settings
from services.agent_gateway_v3.app.core.errors import ApiError
from services.agent_gateway_v3.app.services.agent_registry import get_agent_config, load_registry


@pytest.fixture(autouse=True)
def clear_registry_cache():
    get_settings.cache_clear()
    load_registry.cache_clear()
    yield
    get_settings.cache_clear()
    load_registry.cache_clear()


def test_get_agent_config_returns_maxima_v3() -> None:
    config = get_agent_config("maxima_v3")
    assert config.agent_id == "maxima_v3"
    assert config.backend == "agent_runtime"
    assert "6357932034928672768" in config.resource_name


def test_get_agent_config_returns_maxima_agentruntime_streaming_v3() -> None:
    config = get_agent_config("maxima_agentruntime_streaming_v3")
    assert config.agent_id == "maxima_agentruntime_streaming_v3"
    assert config.backend == "agent_runtime"
    assert config.streaming_enabled is True
    assert "1267738556093169664" in config.resource_name


def test_get_agent_config_returns_maximus88() -> None:
    config = get_agent_config("maximus88")
    assert config.agent_id == "maximus88"
    assert config.backend == "agent_runtime"
    assert config.region == "us-west1"
    assert config.streaming_enabled is True
    assert "2960720930810101760" in config.resource_name


def test_get_agent_config_returns_cloud_run_v3() -> None:
    agent_config = get_agent_config("maxima_cloudrun_v3")

    assert agent_config.agent_id == "maxima_cloudrun_v3"
    assert agent_config.backend == "cloud_run_adk"
    assert agent_config.app_name == "app"
    assert agent_config.runtime_session_cleanup == "cloud_run_adk"


def test_get_agent_config_returns_cloud_run_stream_v3() -> None:
    agent_config = get_agent_config("maxima_cloudrun_stream_v3")

    assert agent_config.agent_id == "maxima_cloudrun_stream_v3"
    assert agent_config.backend == "cloud_run_adk"
    assert agent_config.app_name == "app"
    assert agent_config.streaming_enabled is True
    assert agent_config.runtime_session_cleanup == "cloud_run_adk"


def test_get_agent_config_rejects_unknown_agent() -> None:
    with pytest.raises(ApiError) as exc_info:
        get_agent_config("unknown-agent")
    assert exc_info.value.code == "unknown_agent"


def test_get_agent_config_applies_runtime_env_override(tmp_path, monkeypatch) -> None:
    registry_path = tmp_path / "agents.yaml"
    registry_path.write_text(
        """
agents:
  maxima_v3:
    agent_id: maxima_v3
    resource_name: projects/test/locations/us-east1/reasoningEngines/old
    region: us-east1
    streaming_enabled: false
    persistence_enabled: true
    auth_policy: firebase
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("AGENT_REGISTRY_PATH", str(registry_path))
    monkeypatch.setenv(
        "AGENT_MAXIMA_V3_RESOURCE_NAME",
        "projects/test/locations/us-central1/reasoningEngines/new",
    )
    monkeypatch.setenv("AGENT_MAXIMA_V3_REGION", "us-central1")

    agent_config = get_agent_config("maxima_v3")

    assert agent_config.resource_name == (
        "projects/test/locations/us-central1/reasoningEngines/new"
    )
    assert agent_config.region == "us-central1"


def test_get_agent_config_applies_cloud_run_env_overrides(tmp_path, monkeypatch) -> None:
    registry_path = tmp_path / "agents.yaml"
    registry_path.write_text(
        """
agents:
  maxima_cloudrun_v3:
    agent_id: maxima_cloudrun_v3
    backend: cloud_run_adk
    base_url: https://old.example.run.app
    app_name: old_app
    region: us-east1
    streaming_enabled: false
    persistence_enabled: true
    auth_policy: firebase
    runtime_session_cleanup: none
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("AGENT_REGISTRY_PATH", str(registry_path))
    monkeypatch.setenv(
        "AGENT_MAXIMA_CLOUDRUN_V3_BASE_URL",
        "https://maxima-cloudrun-canary.example.run.app",
    )
    monkeypatch.setenv("AGENT_MAXIMA_CLOUDRUN_V3_APP_NAME", "maxima_cloudrun")
    monkeypatch.setenv("AGENT_MAXIMA_CLOUDRUN_V3_AUDIENCE", "https://audience.example")
    monkeypatch.setenv("AGENT_MAXIMA_CLOUDRUN_V3_REGION", "us-central1")

    agent_config = get_agent_config("maxima_cloudrun_v3")

    assert agent_config.base_url == "https://maxima-cloudrun-canary.example.run.app"
    assert agent_config.app_name == "maxima_cloudrun"
    assert agent_config.audience == "https://audience.example"
    assert agent_config.region == "us-central1"
