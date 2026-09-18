from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from pydantic import BaseModel

from app.models import ModelDeployment


class ModelMessage(BaseModel):
    role: str
    content: str


@dataclass
class GenerationResult:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    provider_request_id: str | None = None
    inference_started_offset_ms: float | None = None
    inference_latency_ms: float | None = None
    first_token_offset_ms: float | None = None
    completed_offset_ms: float | None = None
    provider_metadata: dict | None = None


class ProviderError(RuntimeError):
    def __init__(self, error_type: str, message: str):
        self.error_type = error_type
        super().__init__(message)


class ModelProvider(ABC):
    @abstractmethod
    def generate(
        self, messages: Sequence[ModelMessage], deployment: ModelDeployment
    ) -> GenerationResult:
        raise NotImplementedError

    def stream(
        self, messages: Sequence[ModelMessage], deployment: ModelDeployment
    ) -> Iterator[str]:
        yield self.generate(messages, deployment).text
