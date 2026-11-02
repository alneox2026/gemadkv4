from datetime import datetime, timezone

import httpx

from services.agent_persistence_worker_v3.app.services.agent_runtime_session_usage_hydrator import (
    AgentRuntimeSessionUsageHydrator,
)


def _event(invocation_id: str, prompt_tokens: int, output_tokens: int) -> dict:
    return {
        "invocationId": invocation_id,
        "rawEvent": {
            "usageMetadata": {
                "promptTokenCount": prompt_tokens,
                "candidatesTokenCount": output_tokens,
                "totalTokenCount": prompt_tokens + output_tokens,
            }
        },
    }


def _retrieve(hydrator: AgentRuntimeSessionUsageHydrator, invocation_id: str | None):
    return hydrator.retrieve_usage(
        resource_name="projects/test-project/locations/us-central1/reasoningEngines/123",
        region="us-central1",
        session_id="session-1",
        invocation_id=invocation_id,
        started_at=datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc),
        completed_at=datetime(2026, 10, 20, 10, 1, tzinfo=timezone.utc),
        model_name="gemini-3.8-flash",
        pricing_model=None,
        model_endpoint_location="global",
    )


def test_retrieves_and_aggregates_only_the_requested_invocation():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.params.get("pageToken"):
            return httpx.Response(
                200,
                json={"sessionEvents": [_event("invocation-1", 100, 20)]},
            )
        return httpx.Response(
            200,
            json={
                "sessionEvents": [
                    _event("other-invocation", 900, 90),
                    _event("invocation-1", 200, 30),
                ],
                "nextPageToken": "page-2",
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    hydrator = AgentRuntimeSessionUsageHydrator(http_client=client)
    hydrator._auth_headers = lambda: {"Authorization": "Bearer test"}

    usage = _retrieve(hydrator, "invocation-1")

    assert usage is not None
    assert usage["token_counts"]["prompt_token_count"] == 300
    assert usage["token_counts"]["candidates_token_count"] == 50
    assert usage["estimated_cost_usd"] > 0
    assert len(requests) == 2
    assert requests[0].url.params.get("filter")
    client.close()


def test_does_not_price_usage_when_event_pagination_is_truncated():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "sessionEvents": [_event("invocation-1", 100, 20)],
                "nextPageToken": f"page-{calls + 1}",
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    hydrator = AgentRuntimeSessionUsageHydrator(http_client=client)
    hydrator._auth_headers = lambda: {"Authorization": "Bearer test"}

    assert _retrieve(hydrator, "invocation-1") is None
    assert calls == 2
    client.close()


def test_does_not_guess_when_invocation_id_is_ambiguous():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "sessionEvents": [
                    _event("invocation-1", 100, 20),
                    _event("invocation-2", 200, 30),
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    hydrator = AgentRuntimeSessionUsageHydrator(http_client=client)
    hydrator._auth_headers = lambda: {"Authorization": "Bearer test"}

    assert _retrieve(hydrator, None) is None
    client.close()
