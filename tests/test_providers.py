import json

import httpx
import pytest

from app.config import Settings
from app.models import ModelDeployment
from app.providers.base import ModelMessage, ProviderError
from app.providers.http import HttpModelProvider
from app.providers.mock import MockModelProvider


def deployment():
    return ModelDeployment(
        provider="http",
        model_id="model-a",
        model_version="v7",
        endpoint_reference="https://models.example/generate",
        configuration_json={"temperature": 0.2},
        active=True,
    )


def test_mock_provider_integrates_with_conversation_contract():
    result = MockModelProvider().generate(
        [ModelMessage(role="user", content="hello")], deployment()
    )
    assert result.text == "Mock response: hello"
    assert result.output_tokens is not None


def test_http_provider_sends_server_side_auth_and_parses_metrics():
    def handler(request: httpx.Request):
        assert request.headers["Authorization"] == "Bearer provider-secret"
        assert json.loads(request.content)["messages"] == [{"role": "user", "content": "hello"}]
        return httpx.Response(
            200,
            json={
                "text": "raw answer",
                "request_id": "remote-1",
                "usage": {"input_tokens": 4, "output_tokens": 2},
                "timing": {"inference_ms": 20, "time_to_first_token_ms": 5},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = HttpModelProvider(
        Settings(model_api_key="provider-secret", model_endpoint="https://fallback.example"),
        client=client,
    )
    result = provider.generate([ModelMessage(role="user", content="hello")], deployment())

    assert result.text == "raw answer"
    assert result.provider_request_id == "remote-1"
    assert result.input_tokens == 4
    assert result.output_tokens == 2
    assert result.first_token_offset_ms == 5
    assert result.inference_latency_ms == 20
    assert result.completed_offset_ms is None


def test_http_provider_classifies_timeout_and_malformed_response():
    timeout_client = httpx.Client(
        transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("x")))
    )
    provider = HttpModelProvider(Settings(), client=timeout_client)
    with pytest.raises(ProviderError, match="timed out") as timeout:
        provider.generate([ModelMessage(role="user", content="x")], deployment())
    assert timeout.value.error_type == "timeout"

    malformed_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"unexpected": True})
        )
    )
    provider = HttpModelProvider(Settings(), client=malformed_client)
    with pytest.raises(ProviderError) as malformed:
        provider.generate([ModelMessage(role="user", content="x")], deployment())
    assert malformed.value.error_type == "malformed_response"


def test_http_provider_classifies_remote_status_without_leaking_body():
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, text="internal secret diagnostics")
        )
    )
    provider = HttpModelProvider(Settings(), client=client)
    with pytest.raises(ProviderError) as failure:
        provider.generate([ModelMessage(role="user", content="x")], deployment())
    assert failure.value.error_type == "provider_failure"
    assert "secret" not in str(failure.value)


@pytest.mark.parametrize(
    "body",
    [
        ["not", "an", "object"],
        {"text": "ok", "usage": []},
        {"text": "ok", "usage": "tokens"},
        {"text": "ok", "timing": []},
        {"text": "ok", "timing": "fast"},
        {"text": "ok", "usage": {"input_tokens": True}},
        {"text": "ok", "usage": {"input_tokens": 1.5}},
        {"text": "ok", "usage": {"output_tokens": -1}},
        {"text": "ok", "timing": {"inference_ms": True}},
        {"text": "ok", "timing": {"inference_ms": -0.1}},
        {"text": "ok", "timing": {"time_to_first_token_ms": float("inf")}},
        {"text": "ok", "timing": {"time_to_first_token_ms": float("nan")}},
        {"text": "ok", "request_id": 123},
        {"text": "ok", "metadata": []},
    ],
)
def test_http_provider_rejects_malformed_response_shapes_and_metrics(body):
    encoded_body = json.dumps(body).encode()
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=encoded_body, headers={"Content-Type": "application/json"}
            )
        )
    )
    provider = HttpModelProvider(Settings(), client=client)

    with pytest.raises(ProviderError, match="malformed data") as malformed:
        provider.generate([ModelMessage(role="user", content="x")], deployment())

    assert malformed.value.error_type == "malformed_response"
