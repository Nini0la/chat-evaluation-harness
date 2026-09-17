import math
from collections.abc import Sequence

import httpx

from app.config import Settings
from app.models import ModelDeployment
from app.providers.base import GenerationResult, ModelMessage, ModelProvider, ProviderError


class HttpModelProvider(ModelProvider):
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.model_timeout_seconds)

    def generate(
        self, messages: Sequence[ModelMessage], deployment: ModelDeployment
    ) -> GenerationResult:
        endpoint = deployment.endpoint_reference or self.settings.model_endpoint
        if not endpoint:
            raise ProviderError("configuration_error", "No model endpoint configured")
        headers = {"Content-Type": "application/json"}
        if self.settings.model_api_key:
            headers["Authorization"] = f"Bearer {self.settings.model_api_key.get_secret_value()}"
        payload = {
            "model": deployment.model_id,
            "model_version": deployment.model_version,
            "messages": [message.model_dump() for message in messages],
            "configuration": deployment.configuration_json or {},
        }
        try:
            response = self.client.post(endpoint, json=payload, headers=headers)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ProviderError("timeout", "Remote model request timed out") from exc
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                "provider_failure", f"Remote model returned HTTP {exc.response.status_code}"
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderError("network_failure", "Remote model network request failed") from exc
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("response must be an object")
            text = body.get("text")
            if not isinstance(text, str):
                raise ValueError("missing text")
            usage = body.get("usage", {})
            timing = body.get("timing", {})
            if not isinstance(usage, dict) or not isinstance(timing, dict):
                raise ValueError("usage and timing must be objects")
            inference_ms = timing.get("inference_ms")
            ttft_ms = timing.get("time_to_first_token_ms")
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            token_counts = (input_tokens, output_tokens)
            if any(
                value is not None
                and (isinstance(value, bool) or not isinstance(value, int) or value < 0)
                for value in token_counts
            ):
                raise ValueError("invalid token counts")
            timing_values = (inference_ms, ttft_ms)
            if any(
                value is not None
                and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                )
                for value in timing_values
            ):
                raise ValueError("invalid timing metrics")
            request_id = body.get("request_id")
            metadata = body.get("metadata")
            if request_id is not None and not isinstance(request_id, str):
                raise ValueError("request_id must be a string")
            if metadata is not None and not isinstance(metadata, dict):
                raise ValueError("metadata must be an object")
            return GenerationResult(
                text=text,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                provider_request_id=request_id,
                first_token_offset_ms=ttft_ms,
                inference_latency_ms=inference_ms,
                provider_metadata=metadata,
            )
        except (ValueError, TypeError) as exc:
            raise ProviderError(
                "malformed_response", "Remote model returned malformed data"
            ) from exc
