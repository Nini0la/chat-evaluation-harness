import time
from collections.abc import Sequence
from urllib.parse import quote

import httpx

from app.config import Settings
from app.models import ModelDeployment
from app.providers.base import GenerationResult, ModelMessage, ModelProvider, ProviderError


class AzureOpenAIProvider(ModelProvider):
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.model_timeout_seconds)

    def _url(self, deployment: ModelDeployment) -> str:
        endpoint = deployment.endpoint_reference
        if not endpoint:
            raise ProviderError("configuration_error", "No Azure OpenAI endpoint configured")
        if "/chat/completions" in endpoint:
            return endpoint
        model = quote(deployment.model_id, safe="")
        return f"{endpoint.rstrip('/')}/openai/deployments/{model}/chat/completions"

    def generate(
        self, messages: Sequence[ModelMessage], deployment: ModelDeployment
    ) -> GenerationResult:
        if self.settings.azure_openai_api_key is None:
            raise ProviderError("configuration_error", "AZURE_OPENAI_API_KEY is not configured")
        configuration = dict(deployment.configuration_json or {})
        api_version = configuration.pop("api_version", self.settings.azure_openai_api_version)
        if configuration.get("response_format") == "json":
            configuration["response_format"] = {"type": "json_object"}
        payload = {**configuration, "messages": [message.model_dump() for message in messages]}
        started = time.perf_counter()
        try:
            response = self.client.post(
                self._url(deployment),
                params={"api-version": api_version},
                headers={
                    "Content-Type": "application/json",
                    "api-key": self.settings.azure_openai_api_key.get_secret_value(),
                },
                json=payload,
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "timeout", "Azure OpenAI request timed out", retryable=True
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                "provider_failure",
                f"Azure OpenAI returned HTTP {exc.response.status_code}",
                retryable=exc.response.status_code == 429 or exc.response.status_code >= 500,
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderError(
                "network_failure", "Azure OpenAI network request failed", retryable=True
            ) from exc
        elapsed_ms = (time.perf_counter() - started) * 1000
        try:
            body = response.json()
            choice = body["choices"][0]
            text = choice["message"]["content"]
            if not isinstance(text, str):
                raise ValueError("missing message content")
            usage = body.get("usage") or {}
            if not isinstance(usage, dict):
                raise ValueError("usage must be an object")
            input_tokens = usage.get("prompt_tokens")
            output_tokens = usage.get("completion_tokens")
            if any(
                value is not None
                and (isinstance(value, bool) or not isinstance(value, int) or value < 0)
                for value in (input_tokens, output_tokens)
            ):
                raise ValueError("invalid token counts")
            metadata = {
                "finish_reason": choice.get("finish_reason"),
                "content_filter_results": choice.get("content_filter_results"),
                "model": body.get("model"),
                "system_fingerprint": body.get("system_fingerprint"),
            }
            return GenerationResult(
                text=text,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                provider_request_id=response.headers.get("x-request-id") or body.get("id"),
                inference_latency_ms=elapsed_ms,
                completed_offset_ms=elapsed_ms,
                provider_metadata={
                    key: value for key, value in metadata.items() if value is not None
                },
            )
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise ProviderError(
                "malformed_response", "Azure OpenAI returned malformed data"
            ) from exc

    def close(self) -> None:
        self.client.close()
