import json

import httpx
import pytest
from openai import AsyncOpenAI, InternalServerError

from app.agents import OpenAIProvider
from app.contracts import Outcome, Role
from app.coordinator import Coordinator
from examples.backend_fixture import FixtureProvider, fixture_planner
from tests.conftest import start_run


@pytest.mark.parametrize("api_mode", ["responses", "chat_completions"])
async def test_sdk_uses_six_independent_strict_requests_and_records_usage(store, api_mode):
    requests = []
    fixture = FixtureProvider()

    async def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path == (
            "/v1/responses" if api_mode == "responses" else "/v1/chat/completions"
        )
        messages = body["input"] if api_mode == "responses" else body["messages"]
        context = json.loads(messages[1]["content"])
        reply = await fixture.generate(
            Role(context["role"]), context["round"], {"context": context}, 12
        )
        if api_mode == "chat_completions":
            return httpx.Response(
                200,
                json={
                    "id": f"chatcmpl_{len(requests)}",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "test-model-version",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": reply.text,
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                },
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
        provider = OpenAIProvider("test-only", "test-model", client=client, api_mode=api_mode)
        run = start_run(store, provider)
        result = await Coordinator(store, fixture_planner(), provider).run(run.run_id)
    assert result.negotiation_outcome == Outcome.CONSENSUS
    assert result.model_call_count == len(requests) == 6
    field = "input" if api_mode == "responses" else "messages"
    for number in (1, 2):
        systems = {
            request[field][0]["content"]
            for request in requests
            if json.loads(request[field][1]["content"])["round"] == number
        }
        assert len(systems) == 3
    for body in requests:
        assert body["max_output_tokens" if api_mode == "responses" else "max_tokens"] == 500
        assert body["store"] is False
        if api_mode == "responses":
            schema = body["text"]["format"]
            assert schema["type"] == "json_schema"
        else:
            assert body["response_format"]["type"] == "json_schema"
            assert body["n"] == 1
            assert body["reasoning_effort"] == "none"
            schema = body["response_format"]["json_schema"]
        assert schema["strict"] is True
        assert schema["schema"]["additionalProperties"] is False
        assert set(schema["schema"]["required"]) == set(schema["schema"]["properties"])
        system_schema = body[field][0]["content"].split("Required output schema:\n")[1]
        assert json.loads(system_schema) == schema["schema"]
    trace = store.export(run.run_id)
    assert sum(r["output_tokens"] for r in trace["requests"]) == 300
    assert {r["actual_model"] for r in trace["requests"]} == {"test-model-version"}
    assert "test-only" not in json.dumps(trace)


@pytest.mark.parametrize("api_mode", ["responses", "chat_completions"])
async def test_sdk_never_retries_server_error_even_when_client_default_allows_it(api_mode):
    count = 0

    def fail(request):
        nonlocal count
        count += 1
        return httpx.Response(500, json={"error": {"message": "unavailable"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http:
        client = AsyncOpenAI(api_key="test-only", http_client=http, max_retries=2)
        provider = OpenAIProvider("test-only", "test-model", client=client, api_mode=api_mode)
        with pytest.raises(InternalServerError):
            await provider.generate(Role.ATC, 1, {"system": "test", "context": {}}, 0.1)
    assert count == 1


@pytest.mark.parametrize("finish", ["length", "content_filter", "tool_calls", "refused", "empty"])
async def test_chat_refusals_and_incomplete_outputs_cannot_be_completed(finish):
    def respond(request):
        choice = {
            "index": 0,
            "finish_reason": "stop" if finish == "refused" else finish,
            "message": {
                "role": "assistant",
                "content": "{}",
                "refusal": "Cannot comply" if finish == "refused" else None,
            },
        }
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl_test",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [] if finish == "empty" else [choice],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = AsyncOpenAI(api_key="test-only", http_client=http, max_retries=0)
        provider = OpenAIProvider(
            "test-only", "test-model", client=client, api_mode="chat_completions"
        )
        result = await provider.generate(Role.ATC, 1, {"system": "test", "context": {}}, 0.1)
        assert result.status == finish
        assert result.status != "completed"
