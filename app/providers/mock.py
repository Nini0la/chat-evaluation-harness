from collections.abc import Sequence

from app.models import ModelDeployment
from app.providers.base import GenerationResult, ModelMessage, ModelProvider


class MockModelProvider(ModelProvider):
    def generate(
        self, messages: Sequence[ModelMessage], deployment: ModelDeployment
    ) -> GenerationResult:
        prompt = messages[-1].content if messages else ""
        text = f"Mock response: {prompt}"
        return GenerationResult(
            text=text,
            input_tokens=sum(len(message.content.split()) for message in messages),
            output_tokens=len(text.split()),
            provider_request_id="mock-local",
        )
