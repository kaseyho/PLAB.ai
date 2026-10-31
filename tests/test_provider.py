import json

import httpx
import pytest
from openai import AsyncOpenAI, InternalServerError

from app.agents import OpenAIProvider
from app.contracts import Outcome, Role
from app.coordinator import Coordinator
from examples.backend_fixture import FixtureProvider, fixture_planner
from tests.conftest import start_run


async def test_sdk_uses_six_independent_strict_requests_and_records_usage(store):
    requests = []
    fixture = FixtureProvider()

    async def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path == "/v1/responses"
        context = json.loads(body["input"][1]["content"])
        reply = await fixture.generate(
            Role(context["role"]), context["round"], {"context": context}, 12
        )
        return httpx.Response(
            200,
            json={
                "id": f"resp_{len(requests)}",
                "object": "response",
                "created_at": 1,
                "model": "test-model-version",
                "status": "completed",
                "output": [
                    {
                        "id": "msg",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": reply.text, "annotations": []}],
                    }
                ],
                "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = AsyncOpenAI(api_key="test-only", http_client=http, max_retries=0)
        provider = OpenAIProvider("test-only", "test-model", client=client)
        run = start_run(store, provider)
        result = await Coordinator(store, fixture_planner(), provider).run(run.run_id)
    assert result.negotiation_outcome == Outcome.CONSENSUS
    assert result.model_call_count == len(requests) == 6
    assert len({request["input"][0]["content"] for request in requests}) == 3
    for body in requests:
        assert body["max_output_tokens"] == 500
        assert body["store"] is False
        schema = body["text"]["format"]
        assert schema["type"] == "json_schema" and schema["strict"] is True
        assert schema["schema"]["additionalProperties"] is False
        assert set(schema["schema"]["required"]) == set(schema["schema"]["properties"])
    trace = store.export(run.run_id)
    assert sum(r["output_tokens"] for r in trace["requests"]) == 300
    assert {r["actual_model"] for r in trace["requests"]} == {"test-model-version"}
    assert "test-only" not in json.dumps(trace)


async def test_sdk_never_retries_server_error_even_when_client_default_allows_it():
    count = 0

    def fail(request):
        nonlocal count
        count += 1
        return httpx.Response(500, json={"error": {"message": "unavailable"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http:
        client = AsyncOpenAI(api_key="test-only", http_client=http, max_retries=2)
        provider = OpenAIProvider("test-only", "test-model", client=client)
        with pytest.raises(InternalServerError):
            await provider.generate(Role.ATC, 1, {"system": "test", "context": {}}, 0.1)
    assert count == 1
